#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
东南大学教务处 · 通知自动抓取 + 邮件推送

设计要点：
  * 零第三方依赖（只用 Python 标准库），可以直接扔进 GitHub Actions / 云函数 / 树莓派
  * 抓「栏目列表页」而不是详情页：1 个栏目 1 个请求，开销极小
  * 用 URL 指纹去重，状态存 .state/state.json（可被 git 跟踪，顺便当仓库活跃度）
  * 首次运行静默建基线，不会把历史通知一次性轰到手机上
  * 抓取到 0 条时发告警邮件（防止改版后「静默失效」）

用法：
  python main.py --dry-run      # 只抓取和对比，不发信、不写状态
  python main.py --init         # 建基线：把当前所有通知标记为已见，不发信
  python main.py                # 正式跑：有新增就发一封邮件
  python main.py --test-mail    # 只测 SMTP 配置

环境变量见 README.md 或 config_defaults()。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import smtplib
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate, make_msgid
from pathlib import Path

# --------------------------------------------------------------------------
# 时间
# --------------------------------------------------------------------------
# GitHub Actions / 云函数的服务器都是 UTC 时区，直接用 datetime.now() 会差 8 小时
# （邮件里写着 12:25，实际是晚上 20:25）。这里固定用北京时间，不依赖宿主机时区。
CST = timezone(timedelta(hours=8))


def now_dt() -> datetime:
    return datetime.now(CST)


# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# 栏目名 = 列表页路径。顺序即邮件里的展示顺序。
# 注意：/whszjy/list.htm 是废弃空页，真正的「文化素质教育」在 /cbxx/list.htm
DEFAULT_COLUMNS = [
    ("最新动态",     "/xxtz/list.htm"),
    ("教务信息",     "/jwxx/list.htm"),
    ("学籍管理",     "/xjgl/list.htm"),
    ("教学研究",     "/jxyj/list.htm"),
    ("实践教学",     "/sjjx/list.htm"),
    ("国际交流",     "/gjjl/list.htm"),
    ("文化素质教育", "/cbxx/list.htm"),
]


def _load_dotenv() -> None:
    """本地跑的时候可以从 .env 读配置；GitHub Actions 走 Secrets，不依赖这个文件。"""
    p = Path(".env")
    if not p.exists():
        return
    for line in p.read_text("utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


class Config:
    def __init__(self):
        _load_dotenv()
        env = os.environ.get
        self.base = env("JW_BASE", "https://jwc.seu.edu.cn").rstrip("/")
        self.columns = self._parse_columns(env("JW_COLUMNS", ""))
        self.state_file = Path(env("JW_STATE", ".state/state.json"))
        self.max_pages = int(env("JW_MAX_PAGES", "1"))
        self.retention_days = int(env("JW_RETENTION_DAYS", "400"))
        self.alert_cooldown_h = int(env("JW_ALERT_COOLDOWN_HOURS", "12"))
        self.timeout = int(env("JW_TIMEOUT", "25"))
        self.retries = int(env("JW_RETRIES", "3"))

        self.smtp_host = env("SMTP_HOST", "smtp.qq.com")
        self.smtp_port = int(env("SMTP_PORT", "465"))
        self.smtp_user = env("SMTP_USER", "")
        self.smtp_pass = env("SMTP_PASS", "")
        self.mail_to = env("MAIL_TO", "") or self.smtp_user
        self.mail_from_name = env("MAIL_FROM_NAME", "教务处通知")

    @staticmethod
    def _parse_columns(raw: str):
        raw = (raw or "").strip()
        if not raw:
            return list(DEFAULT_COLUMNS)
        out = []
        for part in raw.split(","):
            part = part.strip()
            if not part:
                continue
            if "=" in part:
                name, path = part.split("=", 1)
                out.append((name.strip(), path.strip()))
            else:
                out.append((part, part))
        return out

    def mail_ready(self) -> bool:
        return bool(self.smtp_user and self.smtp_pass and self.mail_to)


# --------------------------------------------------------------------------
# 日志
# --------------------------------------------------------------------------

def log(msg: str) -> None:
    print(f"[{now_dt():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


# --------------------------------------------------------------------------
# 抓取
# --------------------------------------------------------------------------

def decode_body(resp, raw: bytes) -> str:
    """webplus 的页面是 UTF-8，但老站点可能是 GBK，这里做一次稳健全的探测。"""
    if raw[:3] == b"\xef\xbb\xbf":
        return raw[3:].decode("utf-8", "ignore")
    ctype = (resp.headers.get("Content-Type") or "").lower()
    m = re.search(r"charset=([\w-]+)", ctype)
    candidates = []
    if m:
        candidates.append(m.group(1))
    head = raw[:2048].decode("latin-1", "ignore")
    m2 = re.search(r'charset=["\']?([\w-]+)', head, re.I)
    if m2:
        candidates.append(m2.group(1))
    candidates += ["utf-8", "gb18030"]
    for enc in candidates:
        try:
            text = raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
        if "锟" not in text:          # 典型的 GBK→乱码残留
            return text
    return raw.decode("utf-8", "ignore")


def http_get(url: str, timeout: int = 25, retries: int = 3) -> str:
    last = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Accept-Encoding": "identity",       # 不请求压缩，省掉解压逻辑
                "Connection": "close",
            })
            ctx = ssl.create_default_context()
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                raw = resp.read()
                if resp.status != 200:
                    raise RuntimeError(f"HTTP {resp.status}")
                return decode_body(resp, raw)
        except Exception as exc:                      # noqa: BLE001
            last = exc
            if attempt < retries:
                time.sleep(1.5 * attempt)
    raise RuntimeError(f"抓取失败 {url}: {last}")


# --------------------------------------------------------------------------
# 解析
# --------------------------------------------------------------------------

ANCHOR_RE = re.compile(r"<a\b([^>]*)>(.*?)</a>", re.S | re.I)
HREF_RE = re.compile(r"""href\s*=\s*["']([^"']+)["']""", re.I)
TITLE_RE = re.compile(r"""title\s*=\s*["']([^"']*)["']""", re.I)
DATE_RE = re.compile(r"(20\d{2})\s*[-/年.]\s*(\d{1,2})\s*[-/月.]\s*(\d{1,2})")
TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")
# webplus 详情页固定形态：/2026/1014/c21678a586035/page.htm
DETAIL_RE = re.compile(r"/page\.(?:htm|html)$", re.I)
NOISE = {"更多", "more", "首页", "下一页", "上一页", "尾页"}


def clean_text(s: str) -> str:
    s = TAG_RE.sub("", s)
    s = (s.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<")
         .replace("&gt;", ">").replace("&quot;", '"').replace("&#39;", "'"))
    return WS_RE.sub(" ", s).strip()


def _date_window(html: str, start: int, end: int) -> str:
    """
    取「本条记录的日期」。
    优先锁在同一个 <tr> 内（webplus 列表是表格），避免某条没写日期时
    把下一条的日期偷过来；不是表格布局时退回锚点后 600 字符窗口。
    """
    row_start = html.rfind("<tr", max(0, start - 3000), start)
    row_end = html.find("</tr>", end)
    if row_start != -1 and row_end != -1 and row_end - row_start < 4000:
        return html[end:row_end]
    return html[end: end + 600]


def parse_list(html: str, base_url: str, column: str) -> list[dict]:
    """
    从栏目列表页里抽出 [{'title','url','date','column'}]。
    不依赖具体 class 名，只依赖 webplus 的链接形态 + 同一条记录里的日期，
    这样即使换模板、换皮肤也不容易整体失效。
    """
    items, seen = [], set()
    for m in ANCHOR_RE.finditer(html):
        attrs, inner = m.group(1), m.group(2)
        hm = HREF_RE.search(attrs)
        if not hm:
            continue
        href = hm.group(1).strip()
        if not DETAIL_RE.search(href):
            continue
        url = urllib.parse.urljoin(base_url, href)
        url = urllib.parse.urldefrag(url)[0]
        if url in seen:
            continue

        tm = TITLE_RE.search(attrs)
        title = clean_text(tm.group(1)) if tm else ""
        if not title:
            title = clean_text(inner)
        if len(title) < 4 or title.lower() in NOISE:
            continue

        # 日期通常在紧随其后的 <td> 里
        dm = DATE_RE.search(_date_window(html, m.start(), m.end()))
        date = f"{dm.group(1)}-{int(dm.group(2)):02d}-{int(dm.group(3)):02d}" if dm else ""

        seen.add(url)
        items.append({"title": title, "url": url, "date": date, "column": column})
    return items


def fingerprint(url: str) -> str:
    """URL 指纹：统一协议/大小写、去掉 query 与锚点，避免同一条通知算出两个身份。"""
    p = urllib.parse.urlsplit(url)
    key = "https://" + p.netloc.lower() + (p.path or "/")
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------
# 状态
# --------------------------------------------------------------------------

def load_state(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text("utf-8"))
    except Exception as exc:                          # noqa: BLE001
        log(f"状态文件损坏，按空状态处理：{exc}")
        return {}


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), "utf-8")
    tmp.replace(path)


def prune(state: dict, retention_days: int) -> None:
    cutoff = (now_dt() - timedelta(days=retention_days)).strftime("%Y-%m-%d")
    seen = state.get("seen", {})
    for fp in [k for k, v in seen.items() if len(v) > 4 and v[4] < cutoff]:
        del seen[fp]


# --------------------------------------------------------------------------
# 邮件
# --------------------------------------------------------------------------

def build_mail(new_items: list[dict], total: int, columns: list[str]):
    now = now_dt()
    n = len(new_items)
    subject = f"【东南大学教务处】新增 {n} 条通知 · {now:%m-%d %H:%M}"

    rows = []
    for it in new_items:
        date = it.get("date") or "—"
        rows.append(
            f'<tr>'
            f'<td style="padding:10px 8px;border-bottom:1px solid #eee;white-space:nowrap;'
            f'color:#888;font-size:12px">{html_escape(it["column"])}</td>'
            f'<td style="padding:10px 8px;border-bottom:1px solid #eee;line-height:1.5">'
            f'<a href="{html_escape(it["url"])}" style="color:#0b5faa;text-decoration:none;'
            f'font-size:15px">{html_escape(it["title"])}</a></td>'
            f'<td style="padding:10px 8px;border-bottom:1px solid #eee;white-space:nowrap;'
            f'color:#888;font-size:12px">{date}</td>'
            f'</tr>'
        )

    html = f"""<div style="font-family:-apple-system,'Segoe UI','Microsoft YaHei',sans-serif;
max-width:760px;margin:0 auto;color:#222">
  <h2 style="font-size:18px;margin:0 0 4px">教务处有新通知了</h2>
  <p style="color:#888;font-size:13px;margin:0 0 16px">
    共 {n} 条新增 · 本轮扫描 {total} 条 · {now:%Y-%m-%d %H:%M}
  </p>
  <table style="width:100%;border-collapse:collapse;border-top:2px solid #0b5faa">
    {''.join(rows)}
  </table>
  <p style="color:#aaa;font-size:12px;margin-top:20px">
    监控栏目：{'、'.join(columns)}<br>
    本邮件由 jwc-notify 自动发送，无需回复。
  </p>
</div>"""

    text = "\n".join(
        [f"教务处新增 {n} 条通知（{now:%Y-%m-%d %H:%M}）", ""] +
        [f"- [{it['column']}] {it['title']}  {it.get('date', '')}\n  {it['url']}" for it in new_items]
    )
    return subject, html, text


def html_escape(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def send_mail(cfg: Config, subject: str, html: str, text: str) -> None:
    msg = MIMEMultipart("alternative")
    msg["From"] = formataddr((str(Header(cfg.mail_from_name, "utf-8")), cfg.smtp_user))
    msg["To"] = cfg.mail_to
    msg["Subject"] = Header(subject, "utf-8")
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=cfg.smtp_user.split("@")[-1] or "jwc-notify")
    msg.attach(MIMEText(text, "plain", "utf-8"))
    msg.attach(MIMEText(html, "html", "utf-8"))

    ctx = ssl.create_default_context()
    if cfg.smtp_port == 465:
        server = smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, timeout=30, context=ctx)
    else:
        server = smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=30)
        server.starttls(context=ctx)
    try:
        server.login(cfg.smtp_user, cfg.smtp_pass)
        server.sendmail(cfg.smtp_user, [cfg.mail_to], msg.as_string())
    finally:
        try:
            server.quit()
        except Exception:                              # noqa: BLE001
            pass


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------

def crawl(cfg: Config) -> tuple[list[dict], list[str]]:
    all_items, errors = [], []
    for name, path in cfg.columns:
        base_url = f"{cfg.base}{path}"
        got = 0
        for page in range(1, max(1, cfg.max_pages) + 1):
            url = base_url if page == 1 else re.sub(r"list\.htm$", f"list{page}.htm", base_url)
            try:
                html = http_get(url, cfg.timeout, cfg.retries)
                items = parse_list(html, cfg.base, name)
                all_items.extend(items)
                got += len(items)
            except Exception as exc:                   # noqa: BLE001
                errors.append(f"{name} 第{page}页: {exc}")
                break
        log(f"  {name:<8} {got:>3} 条")
    return all_items, errors


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="东南大学教务处通知抓取与邮件推送")
    ap.add_argument("--init", action="store_true", help="建基线：把当前通知全部标记为已见，不发信")
    ap.add_argument("--dry-run", action="store_true", help="只抓取对比，不发信也不写状态")
    ap.add_argument("--test-mail", action="store_true", help="只测试邮件配置")
    ap.add_argument("--preview", metavar="FILE", help="把本轮会发出的邮件渲染成 HTML 文件，不发信")
    ap.add_argument("--max-pages", type=int, default=None, help="每个栏目抓几页（默认 1）")
    args = ap.parse_args(argv)

    cfg = Config()
    if args.max_pages:
        cfg.max_pages = args.max_pages

    if args.test_mail:
        if not cfg.mail_ready():
            log("缺少 SMTP_USER / SMTP_PASS / MAIL_TO，无法测试")
            return 2
        subject, html, text = build_mail(
            [{"title": "这是一封测试邮件，看到它说明 SMTP 配置正确",
              "url": cfg.base, "date": now_dt().strftime("%Y-%m-%d"),
              "column": "测试"}], 1, [c[0] for c in cfg.columns])
        send_mail(cfg, "【测试】" + subject, html, text)
        log(f"测试邮件已发送至 {cfg.mail_to}")
        return 0

    state = load_state(cfg.state_file)
    seen = state.setdefault("seen", {})
    first_run = not seen

    log(f"开始扫描 {len(cfg.columns)} 个栏目，每栏目 {cfg.max_pages} 页")
    items, errors = crawl(cfg)
    for e in errors:
        log(f"  ! {e}")
    log(f"共解析 {len(items)} 条，去重后 {len({fingerprint(i['url']) for i in items})} 个不同链接")

    if not items:
        log("解析结果为 0 条 —— 大概率是页面改版导致选择器失效，需要立即排查")
        if not args.dry_run and cfg.mail_ready():
            _alert(cfg, state, "教务处通知解析为 0 条，可能页面改版了，请检查 jwc-notify")
        return 1

    unique = {}
    for it in items:
        unique.setdefault(fingerprint(it["url"]), it)

    new = [it for fp, it in unique.items() if fp not in seen]
    new.sort(key=lambda x: (x.get("date") or "", [c[0] for c in cfg.columns].index(x["column"])
                            if x["column"] in [c[0] for c in cfg.columns] else 99), reverse=True)

    if args.dry_run:
        log(f"[dry-run] 本轮发现新增 {len(new)} 条（未写状态、未发信）")
        for it in new[:20]:
            log(f"    + [{it['column']}] {it['date']} {it['title']}")
        return 0

    if args.preview:
        sample = new or list(unique.values())[:12]
        subject, html, _ = build_mail(sample, len(unique), [c[0] for c in cfg.columns])
        Path(args.preview).write_text(
            f"<!-- 主题：{subject} -->\n" + html, "utf-8")
        log(f"已生成邮件预览 {args.preview}（{len(sample)} 条），用浏览器打开即可看效果")
        return 0

    if args.init or first_run:
        for fp, it in unique.items():
            seen[fp] = [it["title"], it["url"], it.get("date", ""), it["column"],
                        now_dt().strftime("%Y-%m-%d")]
        state["updated"] = now_dt().isoformat(timespec="seconds")
        prune(state, cfg.retention_days)
        save_state(cfg.state_file, state)
        if args.init:
            log(f"已建立基线，记录 {len(unique)} 条历史通知，未发送任何邮件")
            return 0
        log(f"首次运行：已记录 {len(unique)} 条历史通知作为基线")
        if cfg.mail_ready():
            _send_startup(cfg, unique, len(items))
            log("已发送「监控已启动」邮件")
        return 0

    if not new:
        log("没有新增通知，本轮不发信")
        return 0

    subject, html, text = build_mail(new, len(unique), [c[0] for c in cfg.columns])
    if not cfg.mail_ready():
        log("缺少 SMTP 配置，无法发信；本轮不写状态（下轮会重试）")
        return 2
    try:
        send_mail(cfg, subject, html, text)
    except Exception as exc:                           # noqa: BLE001
        log(f"发信失败：{exc} —— 本轮不写状态，下轮重试")
        return 1

    for it in new:
        fp = fingerprint(it["url"])
        seen[fp] = [it["title"], it["url"], it.get("date", ""), it["column"],
                    now_dt().strftime("%Y-%m-%d")]
    state["updated"] = now_dt().isoformat(timespec="seconds")
    prune(state, cfg.retention_days)
    save_state(cfg.state_file, state)
    log(f"已推送 {len(new)} 条新增通知并保存状态")
    for it in new:
        log(f"    + [{it['column']}] {it['date']} {it['title']}")
    return 0


def _send_startup(cfg: Config, unique: dict, total: int) -> None:
    items = sorted([it for it in unique.values()],
                   key=lambda x: x.get("date") or "", reverse=True)
    subject = f"【东南大学教务处】监控已启动 · 当前收录 {len(unique)} 条"
    html_items = items[:10]
    _, html, text = build_mail(html_items, total, [c[0] for c in cfg.columns])
    html = html.replace("教务处有新通知了", "监控已启动，以下是最近的 10 条").replace(
        f"共 {len(html_items)} 条新增", f"已收录 {len(unique)} 条，此处只列出最近 10 条")
    send_mail(cfg, subject, html, text)


def _alert(cfg: Config, state: dict, subject: str) -> None:
    last = state.get("last_alert", "")
    cd = timedelta(hours=cfg.alert_cooldown_h)
    if last:
        try:
            prev = datetime.fromisoformat(last)
            if prev.tzinfo is None:        # 兼容旧版本写下的、不带时区的记录
                prev = prev.replace(tzinfo=CST)
            if now_dt() - prev < cd:
                log("告警冷却中，本次不重复发送")
                return
        except (ValueError, TypeError):
            pass
    try:
        send_mail(cfg, f"【告警】{subject}",
                  f"<p style='font-family:sans-serif'>{html_escape(subject)}</p>",
                  subject)
        state["last_alert"] = now_dt().isoformat(timespec="seconds")
        save_state(cfg.state_file, state)
        log("已发送告警邮件")
    except Exception as exc:                           # noqa: BLE001
        log(f"告警邮件发送失败：{exc}")


if __name__ == "__main__":
    sys.exit(main())
