# jwc-notify · 东南大学教务处通知 → 手机邮件

每 15 分钟检查东南大学教务处 7 个栏目，**只要出现新通知就发一封邮件**到你手机。
零第三方依赖（只用 Python 标准库），零成本跑在 GitHub Actions 上，**不需要你开机、不需要买服务器**。

```
GitHub Actions 定时唤醒
      → 抓 7 个栏目列表页（7 个请求，约 2 秒）
      → 解析出标题/日期/链接，生成 URL 指纹
      → 和 .state/state.json 比对，筛出没见过的新通知
      → 合并成一封 HTML 邮件 → QQ 邮箱 SMTP 发出
      → 状态提交回仓库（下次接着去重）
```

---

## 一、5 分钟上线

### 步骤 1：建仓库并上传文件

在 GitHub 新建一个仓库（**建议 Public**，见文末「额度说明」），把本目录所有文件传上去：

```
main.py
README.md
.gitignore
run.bat
.github/workflows/notify.yml
```

> `.state/state.json` 会在第一次运行后自动生成并提交，现在没有是正常的。

### 步骤 2：配置 3 个 Secrets

仓库 → **Settings → Secrets and variables → Actions → New repository secret**：

| Name | Value | 说明 |
|---|---|---|
| `SMTP_USER` | `3282491159@qq.com` | 用哪个邮箱发信 |
| `SMTP_PASS` | QQ 邮箱的**授权码**（16 位） | **不是 QQ 密码**，见下方获取方法 |
| `MAIL_TO` | `3282491159@qq.com` | 收到哪个邮箱 |

### 步骤 3：手动跑一次

仓库 → **Actions → 教务处通知推送 → Run workflow**。

第一次运行会：
1. 把当前 124 条历史通知记进状态文件（**不会**给你发 124 条！）
2. 给你发一封「**监控已启动，以下是最近的 10 条**」邮件 —— 收到这封信就说明全链路通了
3. 之后每 15 分钟自动跑，只在真有新通知时才发信

---

## 二、QQ 邮箱授权码怎么拿

1. 电脑浏览器登录 <https://mail.qq.com>
2. 左上角 **设置 → 账号**（新版叫「账号与安全」）
3. 找到 **POP3/IMAP/SMTP 服务** → 开启「**IMAP/SMTP 服务**」
4. 按提示用手机发条短信验证，得到一个 **16 位授权码**
5. 把这个授权码填进 `SMTP_PASS`

> 授权码不是密码，泄露了去邮箱后台「删除授权码」即可作废，不会影响登录密码。

---

## 三、本地调试（可选）

```bat
rem 1) 只抓取和对比，不发信、不写状态 —— 验证能不能连上教务处
python main.py --dry-run

rem 2) 建基线：把当前通知全部记为已见，不发信
python main.py --init

rem 3) 单独测邮件配置对不对
set SMTP_USER=3282491159@qq.com
set SMTP_PASS=你的授权码
set MAIL_TO=3282491159@qq.com
python main.py --test-mail

rem 4) 把邮件渲染成 HTML 看一眼长什么样
python main.py --preview preview.html
```

Windows 用户也可以直接双击 `run.bat`（会在 `.env` 里读配置，格式见下）。

本地配置也可以写在项目根目录的 `.env`：

```ini
SMTP_USER=3282491159@qq.com
SMTP_PASS=abcdefghijklmnop
MAIL_TO=3282491159@qq.com
```

---

## 四、配置项

全部通过环境变量（或 `.env`）覆盖，不写代码：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `JW_BASE` | `https://jwc.seu.edu.cn` | 站点根地址 |
| `JW_COLUMNS` | 见下 | 监控栏目，格式 `名称=路径,名称=路径` |
| `JW_MAX_PAGES` | `1` | 每个栏目抓几页（假期后想补抓可以调 2~3） |
| `JW_STATE` | `.state/state.json` | 状态文件路径 |
| `JW_RETENTION_DAYS` | `400` | 状态里保留多少天的去重记录 |
| `JW_ALERT_COOLDOWN_HOURS` | `12` | 告警邮件冷却时间 |
| `SMTP_HOST` / `SMTP_PORT` | `smtp.qq.com` / `465` | 换 163 邮箱就改成 `smtp.163.com` |
| `SMTP_USER` / `SMTP_PASS` / `MAIL_TO` | 空 | 见上 |

默认监控的 7 个栏目：

| 栏目 | 列表页 |
|---|---|
| 最新动态 | `/xxtz/list.htm` |
| 教务信息 | `/jwxx/list.htm` |
| 学籍管理 | `/xjgl/list.htm` |
| 教学研究 | `/jxyj/list.htm` |
| 实践教学 | `/sjjx/list.htm` |
| 国际交流 | `/gjjl/list.htm` |
| 文化素质教育 | `/cbxx/list.htm` |

**只想收通知、不想看新闻？** 在 Secrets 里加 `JW_COLUMNS`，去掉「最新动态」：

```
JW_COLUMNS=教务信息=/jwxx/list.htm,学籍管理=/xjgl/list.htm,教学研究=/jxyj/list.htm,实践教学=/sjjx/list.htm,国际交流=/gjjl/list.htm,文化素质教育=/cbxx/list.htm
```

---

## 五、常见问题

| 现象 | 原因 / 处理 |
|---|---|
| 手动运行报 `解析结果为 0 条` | 教务处改版了。打开 `main.py` 跑 `--dry-run` 看日志，把 `DETAIL_RE` 或栏目路径改一下（解析器只依赖 `/page.htm` 链接 + 同行日期，通常改路径就够） |
| 报 `HTTP 403` 或超时 | GitHub 的境外 IP 可能被学校 WAF 拦。改用国内免费云函数（腾讯云 SCF / 阿里云 FC），代码一行不用改，把 `main.py` 的 `main()` 包一层 handler 即可 |
| 邮件进了垃圾箱 | 把发件人加入 QQ 邮箱通讯录；手机端 QQ 邮箱 App 允许通知 |
| 手机不响 | 检查手机「QQ邮箱」App 的通知权限；iOS 关掉「专注模式」对邮件 App 的过滤 |
| 收不到通知也没报错 | 说明确实没新通知。可临时把 `JW_MAX_PAGES=2` 或用 `--dry-run` 看解析条数是不是 124 左右 |
| 超过两个月没收到任何信 | GitHub 会自动停用长期无活动的定时任务。到 Actions 页面点一次 **Enable workflow** 即可（有通知时状态提交会自动产生活动，一般不会触发） |

---

## 六、额度说明（重要）

- **Public 仓库**：Actions 分钟数不限，`*/15` 随便跑。
- **Private 仓库**：免费 2000 分钟/月，每个 job 至少按 1 分钟计费。
  `*/15` = 2880 分钟/月 **会超**；请把 `notify.yml` 里的 cron 改成 `"*/30 * * * *"`（1440 分钟，安全）。
- 邮件本身走 SMTP，不消耗任何第三方额度。

---

## 七、设计说明（为什么这么写）

1. **抓列表页，不抓详情页**：7 个请求拿全量标题，快、稳、不招人烦。
2. **URL 指纹去重**：`sha1(去参数、去锚点、统一协议的 URL)`，同一条通知不会推两次。
3. **首跑静默建基线**：否则第一次执行会把 124 条历史通知一次性推给你。
4. **先入库、后发信**：发信失败就不写状态，下一轮自动重试，不会丢消息。
5. **0 条即告警**：改版导致解析失败时能第一时间知道，而不是「安静地失效」。
6. **状态提交回仓库**：不依赖任何外部数据库，顺便让仓库保持活跃、避免定时任务被停用。

---

License: 随便用。数据版权归东南大学教务处，本工具只做个人提醒，邮件中始终保留原文链接。
