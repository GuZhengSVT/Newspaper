# Newspaper

[English](README.md)

可独立部署的新闻方案：按自定义板块检索，经过证据核验和复核，生成网页、Markdown、JSON、RSS，并按任务发送邮件和 Telegram。支持日报、周报、月报及每隔若干天运行。无需保持电脑或聊天会话开启。

仓库只包含通用代码和虚构配置示例。具体关注内容、订阅来源、提示词、模型账号和收件人由每个部署独立设置。

## 安装与配置

需要 Python 3.11+、Node 26.13.0（统一锁定在 `.node-version`）；附带的 VPS 部署还需要 systemd、Caddy。

仓库提供程序代码、模板、静态资源、提示词、测试和配置示例。安装后的依赖不提交到 Git：`node_modules/` 和 `.venv/` 是本地安装目录，克隆仓库或下载源码时不会包含它们。

| 依赖 | 声明文件 | 安装版本锁定文件 |
| --- | --- | --- |
| Python：httpx、Jinja2、jsonschema、markdown-it-py | `pyproject.toml` | `requirements.lock` |
| Node.js：DeepSeek Harness、dsh-free-search、sharp、undici、proxy-agent 及配套依赖 | `package.json` | `package-lock.json` |

先安装 `.node-version` 指定的 Node，CI 和 `package.json` 使用同一版本。

执行 `make install` 会创建 Python 虚拟环境，并依据锁文件下载两套依赖。声明文件和锁文件保留在 Git 中，保证安装版本可复现。只用于提交和推送源码的发布副本无需安装依赖；需要在其中运行检查或程序时再安装。

```bash
git clone https://github.com/GuZhengSVT/Newspaper.git
cd Newspaper
make install
make check test
cp config.example.toml config.local.toml
cp project.example.toml project.local.toml
cp .env.example credentials.env
cp accounts.example.toml accounts.local.toml
```

配置文件分别负责：

| 文件 | 用途 |
| --- | --- |
| `project.local.toml` | 板块分级、检索内容、提示词、筛选、周期、必查来源和首页 |
| `config.local.toml` | 引用项目文件，设置模型接口、存储路径、网站地址、检索预算、推送开关 |
| `credentials.env` | 模型密钥、SMTP 账号、收件人、Telegram Token/Chat ID |
| `accounts.local.toml` | 管理员维护的账号、多个邮箱、一个 Telegram Chat ID、自选板块及对译选项 |

本地文件已被 Git 忽略。完整设置模板见 [project.example.toml](project.example.toml)。运行配置的 `project_file` 相对于运行配置文件解析；提示词文件相对于项目文件解析。模型接口须支持 `thinking.type = "enabled"` 及统筹、执行任务分别使用的 `reasoning_effort = "max"` / `"low"`；仓库不包含个人模型账号或提供商订阅。

```bash
export NEWSPAPER_CONFIG="$PWD/config.local.toml"
export NEWSPAPER_CREDENTIALS="$PWD/credentials.env"
.venv/bin/python -m newspaper.cli check-config
.venv/bin/python -m newspaper.cli settings
.venv/bin/python -m newspaper.cli schedule
.venv/bin/python -m newspaper.cli doctor
mkdir -p ./private
.venv/bin/python -m newspaper.cli preview --manual --output ./private/first-preview
.venv/bin/python -m http.server --bind localhost --directory ./private/first-preview/web/current
```

`check-config`、`settings`、`schedule` 和 `doctor` 不检索、不调用模型、不发送消息。`settings` 显示配置版本、生效模式、预算与凭据就绪状态，隐藏接口地址、提示词、私有目录、密钥和收件地址。`schedule` 展示最近应生成的时段、输出文件标识和推送任务分组。

每次预览使用新的输出目录，使用配置中的检索和模型凭据执行实际采编，在该目录写入独立的数据库和网站。预览不会同步公网或发推送，本地网页服务器只提供 `web/current` 中的文件。`run --no-notify` 正式发布到配置指定的网站并记录刊期，仅跳过该次推送。

立即测试尚未到期的任务时，先用 `.venv/bin/python -m newspaper.cli schedule --manual` 查看全部启用的推送任务，再逐个执行 `.venv/bin/python -m newspaper.cli run --manual --task TASK_ID`。每次命令运行该任务的全部发布内容，并经过原有检索、编辑、核验、建站和推送流程；一个命令结束后再启动下一个。手动刊使用独立发布 ID，日报回溯一日、周报回溯一周，截止于启动时刻。可用带时区的 `--cutoff` 为当天多组手动任务固定相同截止时间；手动运行不修改原定周期、时刻和首次发布日期。

## 板块独立定制

`[[groups]]` 定义一级板块的 `id/name/sections`，`[[sections]]` 定义子板块的 `id/name/scope`。每个启用子板块恰好属于一个启用一级板块。两级都支持 `enabled`、`notify` 和 `schedule`，禁用一级板块会禁用其子板块。

每个子板块可单独设置：

- `search.queries`：固定检索词；留空则依据 scope、keywords、languages 及 plan 提示词规划。支持 `{date}`、`{window_start}`、`{window_end}` 日期占位符。`query_count` 控制规划数量（1–8）。
- `prompts.plan/select/edit/review/repair/compress/collect`：检索规划、初筛、编辑、复核等阶段的附加规则，只作用于对应板块。全局 `[prompts]` 支持直接文本或 `<阶段>_file`，`editorial`/`editorial_file` 用于编辑和证据阶段。不能绕过证据校验。
- `filter.importance`：重大/重要；`reliability`：A/B；`exclude_keywords`：按字面包含关系排除；`require_primary`：核心事实必须使用官方一手证据。
- `format`：普通/重大新闻各自的字符数和段落上限；超长内容经过限定次数的压缩和复核。
- `search.domains`：值得定向补查的域名列表。仅作为规划提示传给检索规划模型，不保证抓取，也不会计入已读来源。
- `sources`：`rss` 支持 RSS/Atom，`index` 从网页目录发现链接（`link_pattern` 可用正则限定文章链接），`page` 查阅该网址本身；`enabled = false` 可临时停用。`required = true` 表示必须成功读取，读取失败会使该份发布失败，不冒充已查阅。
- `discovery`：`max_indexes_per_edition`（目录预算，1–128）、`max_links_per_index`（每个目录取链上限，1–30）、`max_previews_per_section`（每板块进入初筛的候选上限，1–60，按发布者轮转取，不偏向排在前面的来源）、`max_gap_queries`（对已有来源覆盖的板块额外补搜的总量上限，0–40）。

采集顺序是**先读配置来源、后搜索**：所有启用的目录和 feed 会先被读取，其结果与标题摘要交给检索规划，搜索只用于补缺口。`search.queries` 为空的板块若在窗口内没有从来源取得任何条目，则必须至少搜索一次；已取得条目的板块可完全不搜索。只有未被板块规则排除、且同站发布者提供了带时区的窗口内发布时间的链接才计为已覆盖；只有日期、更新时间或搜索摘要的链接仍可参与初筛，读取成功但 24 小时内无更新的源仍算缺口。没有近期条目不等于抓取失败，但来源主题狭窄、关键站点读取失败或内容截断时会优先补查。

规划输入明确列出每板块查询上限，总预算取实际可用容量。不合规规划由程序携带具体约束错误自动修正一次，仍不合规则终止并在运行记录保存错误代码、计数与上限。初筛无合格候选时，在既有抓取预算内检查最多三个不同发布者的原文，再复查候选并审核空刊；原文全部抓取失败仍拒绝发布。证据上下文中的中文实体名未命中英文原文，或只匹配到页面头部时，使用完整的上下文字符预算保留前文，避免在摘要开头就截断核心结果。

候选原文不可读取时，程序在剩余补查及抓取预算内查找同一事件的其他证据，仍须通过原有日期、引文和语义核验。最终复核只覆盖当前成稿；被重要性等规则剔除的候选不再进入逐条复核列表。复核返回的事件列表不匹配时自动纠正一次，空成稿也须结合已抓取原文核对后才能发布。

必查数量超预算会报错，不会静默遗漏；配置总数超过目录预算同样直接报错，不会悄悄跳过排在后面的板块。目录发现和搜索发现的原文共享抓取预算，仍须符合报道时段、筛选和证据复核要求。

## 生成周期与推送任务

设置按全局 `[schedule]` → 一级板块 `[groups.schedule]` → 子板块 `[sections.schedule]` 继承。时间使用 `project.timezone`，例如 `Asia/Shanghai`。

```toml
[sections.schedule]
period = "weekly"
weekday = "mon"
time = "09:00"
# start_date = "2027-01-04"
```

| 周期 | 配置 | 报道范围 |
| --- | --- | --- |
| 日报 `daily` | time | 前一天同一时间至本次时间 |
| 周报 `weekly` | weekday（mon 到 sun）、time | 上周同一时间至本次时间 |
| 月报 `monthly` | month_day（1–31）、time | 上月同一时间至本次时间 |
| 定期 `interval` | every_days（1–30）、必填带引号的 start_date、time | 前 N 天至本次时间 |

周一 09:00 周报会在周一 09:00 开始生成，内容覆盖前一个周一 09:00 至本次周一 09:00，成功后推送。`start_date` 限定最早允许发布的日期，首期仍查阅完整前一周期。月度日期超过当月天数时使用当月最后一天。停机后只补最近一期，不连续生成全部错过的期数。

**各一级板块独立工作、独立推送，按配置顺序串行完成。** `[model] coordination = "configured"` 按项目配置分派并汇总各板块。`coordination = "model"` 由模型以 `max` 推理强度统筹，先分派任务，再根据精简结果决定发布内容。搜索规划、整理筛选、编辑、事实判断、配图和翻译使用独立的 `low` 请求。统筹只接收子板块任务和新闻精简摘要，完整来源、正文留在执行任务中，避免主上下文持续增长。板块内的执行任务也串行调用，当前板块完成并尝试推送后再开始下一个，检索缓存按板块隔离。调用记录包含板块、角色、推理强度和供应商原始用量、缓存字段。

可设置 `[model] max_tokens_per_edition`。同一期的统筹、采编、重试和后台续译合计供应商报告的 `usage.total_tokens`，达到设定值后暂停发起新的模型请求。`status` 可查看各期的 `total_tokens`、`unreported_calls`、配置额度 `limit` 及额度是否已用尽。传输异常、HTTP 失败或缺少有效用量的响应记入 `unreported_calls`，按零计入本期已知用量。触及阈值的单次请求可能超出剩余额度，旧 DSH 采集子进程内部的模型用量也无法取得。供应商退避跨进程保留，探测失败后增加等待时间。启用流程告警时，同一端点连续三次模型调用以可重试故障结束会触发一项告警，直到端点恢复。

推送任务按“一级板块＋周期及起始设置”隔离；即使时间相同，不同一级板块也不合并消息。同一板块内的日报、周报、月报仍分别生成和推送，同一事件可跨刊关联，周报仍可引用日报已核验的证据。同一周期的进展须给出新增事实和新证据。旧历史会迁移到新任务标识，已发布内容不会因此重新推送。

任务先持久记录同一板块、同一周期应生成的内容，完成后独立推送；部分失败或进程重启会继续恢复，不等待其他一级板块。已确认成功的渠道不重复发送，仅对明确失败的渠道按退避间隔重试，最多五次。如果连接中断导致无法确认对方是否已接收，状态设为 `uncertain`，停止自动重发。需通过 `status` 和实际收件情况核实，避免为了重试而重复发送。Telegram 无法提供绝对的端到端恰好一次保证。迁移的已发布内容和使用 `run --no-notify` 发布的内容不会自动补发。

在运行配置中启用邮件或 TG，密钥变量见 [.env.example](.env.example)。SMTP 使用已有邮箱服务，支持 STARTTLS/SSL。推送包含新闻完整正文与来源，末尾附对应网页链接；不发送内部摘要、证据引文和审核记录。邮件包含响应式 HTML 与纯文本两个版本；HTML 沿用网站的纸色底、衬线报头、宋体正文和细线分隔，采用适合手机阅读的单栏布局，中英段落依次对应。Telegram 使用基础 HTML 排版，每条新闻单独成段，来源随正文发送，配图附来源链接；长文按段落及 UTF-16 长度拆分，能容纳的一组中英段落保持在同一条消息中，逐条记录发送进度，明确失败后从未发送部分继续；链接预览关闭，已确认成功的部分不重发。照片单独发送，外链失效或被防盗链拦截时跳过图片，继续发送正文。[Telegram 平台限制](https://core.telegram.org/bots/api#sendmessage)为正文最多 4096 字符、照片说明最多 1024 字符，程序预留余量。`notify = false` 保留网页生成，但从推送内容中排除该板块。

新闻允许可选外链配图，每条最多一张，网页、Markdown、HTML 邮件和 Telegram 均支持。候选只取自本条新闻实际引用的来源页面，并要求发布者提供有信息的图片描述；单独编辑步骤核对具体主体、版本及情境，无法确认关联时留空。程序检查 HTTPS 和重定向地址、解码、文件大小、尺寸及图像细节，排除小图、极端长宽比、空白图和明显模糊图。图注保留示意图等属性及来源；不使用站点标志、广告或无关装饰图。无合适配图或检查失败时保留纯文字新闻，配图不作为正文事实证据，图片二进制仅在校验时暂存内存，不保存本地副本。`batch` 仅用于另一条检索流程的主题分组，当前 `bounded` 模式不使用，也不控制推送。

## 首页与订阅

### 中英对译

`[translation] enabled = true` 在中文新闻通过事实复核后，每篇文章独立调用一次模型，生成英文标题与逐句英文，不再调用模型审核或反馈重译。英文以准确、自然、清晰为目标，保留原稿的归因、限定和不确定性。中文原句由程序直接保留，程序检查译文结构、句子编号、完整性和明确日期。失败记录原因。`publish = "together"` 等待整期译文齐备。`publish = "chinese_first"` 先发布已复核的中文，译文由续译任务按篇补齐。要求双语的收件地址等订阅文章的译文及网页发布就绪后再发送。

分句保留书名号、引号及括号内的标点，避免把作品名中的感叹号切成多句。翻译前保存私有的已审中文、引用证据及逐篇完成的译文；失败重试从此检查点继续，不重新采编或重译已完成的文章。检查点不参与公开网页或消息，发布成功后删除，并随新闻日期到期清理。已发布历史稿的旧分句格式仍可读取。

菜单栏统一提供“中英对译”开关，阅读模式在浏览器中保存，并在首页与往期页面间保持一致；正文板块不再单独设置开关。无译文的文章继续显示中文。**展示形式是一段英文、一段中文**，段内句子依次对应，段落边界与中文阅读相同。默认显示中文；无需浏览时调用模型。翻译与中文一起保存在公开 JSON 的 `article.bilingual` 内；中文 Markdown 和默认 RSS 保持原有用途。

升级后执行 `.venv/bin/python -m newspaper.cli translate` 可为保留期内的历史稿件补齐对译并重建网站，保留日期和事实正文不变，不触发新闻补推送。已成功翻译的文章再次运行会跳过。

### 私有账号推送

运行配置顶层 `accounts_file = "accounts.local.toml"` 指向相对于运行配置的私有 TOML 文件，`[notifications] mode = "accounts"` 启用账号模式。管理员直接编辑该文件；系统没有公开注册、登录或订阅接口。用 `accounts.example.toml` 建立自己的文件，不要把真实收件地址或 Chat ID 提交到 Git。VPS 安装权限为 `root:newspaper 0640`。

每个账号有唯一 `id`、可选 `name/enabled`、`emails` 数组、一个可选的数字字符串 `telegram_chat_id`。TG 用户须先与部署的机器人开始对话。订阅格式如下，填写项目中的一级或子板块 ID：

```toml
[[accounts]]
id = "ExampleReader"
emails = ["reader@example.com", "backup@example.net"]
telegram_chat_id = "123456789"
subscriptions = [
  { section = "research", bilingual = true },
  { section = "culture" },
]
```

`[notifications.defaults] subscriptions = ["*"]` 默认订阅所有启用板块，`bilingual = false` 默认只推中文。账号不填 `subscriptions` 时继承默认范围，填 `[]` 则不订阅任何内容，填规则数组则只订阅数组指定的板块。规则省略 `bilingual` 时继承默认值；子板块规则覆盖一级规则，一级规则覆盖 `*`。关闭账号可设 `enabled = false`。账号未填写的偏好走默认配置；板块生成与推送时刻继续继承全局→一级→子板块的 `schedule`，同一任务全部生成完成后发送。板块或一级 `notify = false` 仍优先禁止推送。

邮箱和 Telegram 只发送该账号订阅的板块；开启对译的板块使用与网站相同的英文段落＋中文段落。网页阅读按钮仅控制当前浏览器，不修改私有账号的推送偏好。邮箱和 Chat ID 未绑定时，该渠道不会发送，也不会借用其他账号或旧的全局收件地址。SMTP 和 Bot Token 仍在 `credentials.env` 共用，渠道开关仍使用 `[notifications.email/telegram] enabled`。

每个账号、每个邮箱及 TG 绑定分别记录发送状态和 TG 分段进度；一个收件地址失败不影响其他地址，已成功的地址不重发，结果不确定的投递停止自动重试。任务准备发送时冻结收件地址与订阅快照；取消账号、地址、订阅或更改对译偏好会取消受影响的延迟重试，以免旧内容误投。新配置用于之后的任务，不回溯补发历史。账号模式会停止旧全局队列的发送；`mode = "legacy"` 可显式回到旧模式。

`.venv/bin/python -m newspaper.cli accounts` 检查生效账号、收件数量及订阅，不输出收件地址；`check-config` 校验 TOML，`status` 查看按账号的投递状态。

可在运行配置设置 `[notifications.failure_alerts] account_id = "ExampleAdmin"`，将流程故障告警仅发送给指定账号绑定的邮箱和 Telegram，使用现有渠道开关，与新闻订阅范围无关。生成任务六小时内失败三次、新闻投递失败五次或流程命令连续失败三次后告警；生成与推送定时任务都会检查。告警包含任务、失败阶段和已记录的原因，每个故障及收件地址去重，已恢复故障不补发，其他账号不接收。告警自身最多退避重试五次，结果不确定时停止重发；`status` 可查看状态。删除此配置停止告警，禁用账号或取消绑定会取消相关待发送告警；`run --no-notify` 不发送告警。配置无法加载或数据库无法打开时，程序无法通过这条应用内告警通道发送通知。

`[homepage] mode = "groups"` 直接展示一级板块正文，`groups` 列表控制显示范围和顺序。`mode = "sections"` 配合 `sections` 列表展示指定子板块；`mode = "index"` 仅显示各期链接。首页按子板块组合最新保留的一期，因此周报/月报会显示到下一次更新。每个板块带原期日期及链接；最新一期为空时不会复活更旧新闻。

首页布局独立于内容模式：`layout = "tabs"`（默认）使用按钮切换板块，`layout = "columns"` 在桌面分栏、手机纵向排列。`layout_switch = true`（默认）显示“首页布局”选项并在浏览器保存读者选择；改为 `false` 则固定使用配置布局。切换按钮支持方向键、Home/End，目录或带锚点的链接会先打开对应板块。关闭 JavaScript 时仍可阅读全部板块。

[examples/politics.toml](examples/politics.toml) 提供可选政治配置，包含中国、美国、欧洲、俄乌战争、中东地区、南美、非洲、其他八个子板块。可将其中的一级板块和子板块合并到私有项目文件，并在 `homepage.groups` 中加入 `politics`；也可作为独立项目使用。来源包括官方发布、联合国，以及 BBC、卫报、巴西通讯社和 CNA。路透社、美联社及相关官方网站作为补搜提示，不计入已读取信源。战争专题优先于地域归类，争议声明须归因、交叉核实。来源访问情况可能变化，非强制来源失败仍需搜索补缺。

界面支持目录、手机布局、明暗模式、归档搜索、Markdown/JSON 下载。目前界面文案与 Schema 枚举采用中文。综合 RSS 为 `/feed.xml`，一级板块 RSS 为 `/feed-<一级ID>.xml`。正文路径为 `/daily/YYYY-MM-DD/<发布ID>/`，下载为 `/downloads/YYYY-MM-DD/<发布ID>.md` 或 `.json`；分周期后的发布 ID 可通过 `schedule` 查看。

## VPS 部署和维护

### 构建和安装源码发布包

`make release REF=<标签或提交>` 导出指定的 Git 提交，写入包含程序版本和完整提交号的 `release.toml`，并在 `dist/` 生成压缩包和 SHA-256 校验文件。需要从标签自动打包时，将 `deploy/github/ci.yml` 安装为 `.github/workflows/ci.yml`。发布包包含源码及锁定依赖清单。

1. 下载所需版本，用 `sha256sum -c <压缩包>.sha256` 校验并解包到 `/opt` 下。systemd 使用该版本目录作为工作目录，目录写权限只授予可信部署管理员。
2. 在解包目录使用固定版本的 Node 和 Python 3.11+ 运行 `make install`。确保服务的 PATH（`/usr/local/bin:/usr/bin:/bin`）可找到固定版本的 Node。
3. 首次安装执行 `bash scripts/install-vps.sh init --dry-run --caddy` 查看计划，升级时把 `init` 改成 `upgrade`。使用 `--caddy` 时，在现有 `/etc/caddy/Caddyfile` 添加一行顶层 `import /etc/caddy/newspaper.caddy`。旧安装先把原有 Newspaper 站点块移到单独源文件，再把主配置中该站点块换成这条导入。用 `--caddy-site /path/to/site.caddy` 指定该文件或配置好域名的片段。编辑期间让 Caddy 继续使用已加载的配置。
4. 用 root 去掉 `--dry-run` 执行安装。升级前先停止 Newspaper 定时器，等正在运行的 Newspaper 服务退出。升级将 systemd 单元指向新版本目录，继续使用原有私有配置及状态数据。启用 Caddy 管理时，安装器先验证组合配置，再替换独立片段。安装完成后再重载 Caddy。由其他服务提供静态源站时可省略 Caddy 选项。

| 路径 | 内容 |
| --- | --- |
| `/opt` 下的解包目录 | 指定提交的源码和本地锁定依赖 |
| `/etc/newspaper/config.toml` | 运行配置 |
| `/etc/newspaper/project.local.toml` | 私有项目偏好 |
| `/etc/newspaper/credentials.env` | 密钥及收件人 |
| `/var/lib/newspaper` | 数据库、新闻、证据、临时目录 |
| `/srv/newspaper/current` | 当前静态网站 |

附带 systemd 单元只为默认状态目录授予写权限。若部署使用自定义路径，在 systemd drop-in 中配置相应的 `ReadWritePaths`，并将静态站点根目录指向所选的 `web_dir/current`。

在解包目录执行以下命令，启用生产调度前先编辑并核对 `/etc/newspaper` 中的配置。

```bash
sudo -u newspaper .venv/bin/python -m newspaper.cli --version
sudo -u newspaper .venv/bin/python -m newspaper.cli check-config
sudo -u newspaper .venv/bin/python -m newspaper.cli settings
sudo -u newspaper .venv/bin/python -m newspaper.cli schedule
sudo -u newspaper .venv/bin/python -m newspaper.cli build
sudo systemctl enable --now newspaper.timer newspaper-clean.timer newspaper-notify.timer
sudo -u newspaper .venv/bin/python -m newspaper.cli status
```

旧配置可省略 `config_version` 和新模式字段，默认使用配置版本 1、本地发布、模型统筹、中英对译齐发。新安装样例选择配置统筹和中文先发。使用 Pages 时，`base_url` 必须是 HTTPS 网站根地址，配置 Pages 工作流后启用 `newspaper-publish.timer`。启用翻译且设为 `translation.publish = "chinese_first"` 时启用 `newspaper-translate.timer`。切换模式时停用对应的可选定时器。`status` 展示应用版本、站点版本和各板块最近一次可用的来源到刊发计数。详见[发布配置与检查](docs/publication.zh-CN.md)。

生成定时器每分钟检查任务，已发布时段直接跳过。单份失败至少等五分钟，六小时内最多三次失败尝试。systemd 单轮最长 50 分钟。推送重试和到期清理各有定时器，远端确认独立于新闻生成。推送链接和 RSS 使用顶层 `base_url`，变更后执行 `build`。附带 Caddy 片段默认监听 80 端口，可按部署设置域名与 HTTPS。

## 核验和保留期限

默认 bounded 模式通过固定版本 DSH 搜索插件完成检索和原文抓取，依次执行规划、初筛、取证、编辑、引文/日期/历史校验、独立模型复核。摘要只用于发现线索，不能作为发布证据；每项事实必须有匹配原文的引文，多源声明还需不同域名及语义独立性复核。限定次数修订后仅发布逐条通过的新闻。检索失败不能当作“无新闻”成功。自动核验仍不能保证事实绝对正确。

仅保留入选来源元数据、引文和相邻上下文。未入选网页、中间稿、DSH 会话/缓存在单次任务结束后删除，异常残留由清理任务回收。运行指标只留计数、结果和用量，不留响应正文或凭据。

保留发布当日及此前规则对应的30个日历日期，以项目时区计算；重建和重试不延长寿命。清理涵盖过期正文、证据、网页、下载、RSS、搜索索引、事件、推送队列、运行记录和旧网站版本，SQLite 使用安全删除并回收空间。直供模式将公开文件保存在 VPS。使用 Pages 时，工作流上传经摘要校验的站点产物，产物保留期设为一天。两种模式均无需把新闻正文提交到 Git。读者下载和第三方服务已保存的副本不受服务器删除控制。

`make check test` 使用合成资料和模拟发送，离线检查日历、板块串行处理与独立推送、防重复、重启恢复、筛选、证据、首页、清理与渲染，不发送真实邮件/TG。保留有用回归测试，仅清理临时探针和缓存。依赖固定在两个 lock 文件中，升级时同步检查 DSH 配置；当前使用 DSH 0.2.1-rc.4，保留并升级 dsh-free-search 至 0.9.17。

MIT 许可证。DSH 与插件保留各自许可证。
