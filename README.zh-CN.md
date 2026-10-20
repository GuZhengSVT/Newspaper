# Newspaper

[English](README.md)

可独立部署的新闻方案：按自定义板块检索，经过证据核验和复核，生成网页、Markdown、JSON、RSS，并按任务发送邮件和 Telegram。支持日报、周报、月报及每隔若干天运行。无需保持电脑或聊天会话开启。

仓库只包含通用代码和虚构配置示例。具体关注内容、订阅来源、提示词、模型账号和收件人由每个部署独立设置。

## 安装与配置

需要 Python 3.11+、Node 22.13+；附带的 VPS 部署还需要 systemd、Caddy。

```bash
git clone https://github.com/GuZhengSVT/Newspaper.git
cd Newspaper
make install
make check test
cp config.example.toml config.local.toml
cp project.example.toml project.local.toml
cp .env.example credentials.env
```

三份文件分别负责：

| 文件 | 用途 |
| --- | --- |
| `project.local.toml` | 板块分级、检索内容、提示词、筛选、周期、必查来源和首页 |
| `config.local.toml` | 引用项目文件，设置模型接口、存储路径、网站地址、检索预算、推送开关 |
| `credentials.env` | 模型密钥、SMTP 账号、收件人、Telegram Token/Chat ID |

本地文件已被 Git 忽略。完整设置模板见 [project.example.toml](project.example.toml)。运行配置的 `project_file` 相对于运行配置文件解析；提示词文件相对于项目文件解析。模型可使用自行选择的兼容接口，仓库不绑定个人模型或提供商。

```bash
export NEWSPAPER_CONFIG="$PWD/config.local.toml"
export NEWSPAPER_CREDENTIALS="$PWD/credentials.env"
export NEWSPAPER_DATA=/tmp/newspaper-data
export NEWSPAPER_WEB=/tmp/newspaper-web
.venv/bin/python -m newspaper.cli check-config
.venv/bin/python -m newspaper.cli schedule
.venv/bin/python -m newspaper.cli doctor
.venv/bin/python -m newspaper.cli build
# 真实检索、调用模型并发布网页，不推送邮件/TG：
.venv/bin/python -m newspaper.cli run --no-notify
```

前三条检查命令不检索、不调用模型、不发送消息。`schedule` 展示最近应生成的时段、输出文件标识和推送任务分组。`--no-notify` 仍会正式发布网页，重复运行跳过已发布内容，之后也不会自动补推送。没有启用板块时暂停生成。

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

采集顺序是**先读配置来源、后搜索**：所有启用的目录和 feed 会先被读取，其结果与标题摘要交给检索规划，搜索只用于补缺口。`search.queries` 为空的板块若在窗口内没有从来源取得任何条目，则必须至少搜索一次；已取得条目的板块可完全不搜索。只有一个来源在窗口内真的产出了条目才算「已覆盖」，读取成功但 24 小时内无更新的源仍算缺口。没有近期条目不等于抓取失败，但来源主题狭窄、关键站点读取失败或内容截断时会优先补查。

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

**推送按“周期＋起始设置”分任务，与一级板块无关。** 周期、时刻、周几/月几日/间隔锚点及可选 start_date 一致的内容合并为一封邮件、一组 TG 消息。日报、周报、月报分别推送；周三周报与周五周报分别推送；相同周期但不同时间也分别推送。同一一级板块内不同周期的子板块分别生成。历史去重按任务隔离，日报中的事件仍可成为独立周报的候选。

任务先持久记录所有应生成的内容，全部完成后推送；部分失败或进程重启会继续等待、恢复。已确认成功的渠道不重复发送，仅对明确失败的渠道按退避间隔重试，最多五次。如果连接中断导致无法确认对方是否已接收，状态设为 `uncertain`，停止自动重发。需通过 `status` 和实际收件情况核实，避免为了重试而重复发送。Telegram 无法提供绝对的端到端恰好一次保证。迁移的已发布内容和无推送预览不会自动补发。

在运行配置中启用邮件或 TG，密钥变量见 [.env.example](.env.example)。SMTP 使用已有邮箱服务，支持 STARTTLS/SSL。推送包含新闻完整正文与来源，末尾附对应网页链接；不发送内部摘要、证据引文和审核记录。邮件包含响应式 HTML 与纯文本两个版本。Telegram 使用基础 HTML 排版，按段落及 UTF-16 长度拆分，逐条记录发送进度，明确失败后从未发送部分继续；链接预览关闭，已确认成功的部分不重发。照片单独发送，外链失效或被防盗链拦截时跳过图片，继续发送正文。[Telegram 平台限制](https://core.telegram.org/bots/api#sendmessage)为正文最多 4096 字符、照片说明最多 1024 字符，程序预留余量。`notify = false` 保留网页生成，但从推送内容中排除该板块。

新闻允许可选外链配图，每条最多一张，网页、Markdown、HTML 邮件和 Telegram 均支持。候选只取自本条新闻实际引用的来源页面，并要求发布者提供有信息的图片描述；单独编辑步骤核对具体主体、版本及情境，无法确认关联时留空。程序检查 HTTPS 和重定向地址、解码、文件大小、尺寸及图像细节，排除小图、极端长宽比、空白图和明显模糊图。图注保留示意图等属性及来源；不使用站点标志、广告或无关装饰图。无合适配图或检查失败时保留纯文字新闻，配图不作为正文事实证据，图片二进制仅在校验时暂存内存，不保存本地副本。`batch` 仅用于另一条检索流程的主题分组，当前 `bounded` 模式不使用，也不控制推送。

## 首页与订阅

`[homepage] mode = "groups"` 直接展示一级板块正文，`groups` 列表控制显示范围和顺序。`mode = "sections"` 配合 `sections` 列表展示指定子板块；`mode = "index"` 仅显示各期链接。首页按子板块组合最新保留的一期，因此周报/月报会显示到下一次更新。每个板块带原期日期及链接；最新一期为空时不会复活更旧新闻。

首页布局独立于内容模式：`layout = "tabs"`（默认）使用按钮切换板块，`layout = "columns"` 在桌面分栏、手机纵向排列。`layout_switch = true`（默认）显示“首页布局”选项并在浏览器保存读者选择；改为 `false` 则固定使用配置布局。切换按钮支持方向键、Home/End，目录或带锚点的链接会先打开对应板块。关闭 JavaScript 时仍可阅读全部板块。

[examples/politics.toml](examples/politics.toml) 提供可选政治配置，包含中国、美国、欧洲、俄乌战争、中东地区、南美、非洲、其他八个子板块。可将其中的一级板块和子板块合并到私有项目文件，并在 `homepage.groups` 中加入 `politics`；也可作为独立项目使用。来源包括官方发布、联合国，以及 BBC、卫报、巴西通讯社和 CNA。路透社、美联社及相关官方网站作为补搜提示，不计入已读取信源。战争专题优先于地域归类，争议声明须归因、交叉核实。来源访问情况可能变化，非强制来源失败仍需搜索补缺。

界面支持目录、手机布局、明暗模式、归档搜索、Markdown/JSON 下载。目前界面文案与 Schema 枚举采用中文。综合 RSS 为 `/feed.xml`，一级板块 RSS 为 `/feed-<一级ID>.xml`。正文路径为 `/daily/YYYY-MM-DD/<发布ID>/`，下载为 `/downloads/YYYY-MM-DD/<发布ID>.md` 或 `.json`；分周期后的发布 ID 可通过 `schedule` 查看。

## VPS 部署和维护

源码放在 `/opt/Newspaper` 并安装依赖，运行 `sudo bash scripts/install-vps.sh`，再修改 `/etc/newspaper` 内的配置后启用定时器。

| 路径 | 内容 |
| --- | --- |
| `/opt/Newspaper` | 通用源码及依赖 |
| `/etc/newspaper/config.toml` | 运行配置 |
| `/etc/newspaper/project.local.toml` | 私有项目偏好 |
| `/etc/newspaper/credentials.env` | 密钥及收件人 |
| `/var/lib/newspaper` | 数据库、新闻、证据、临时目录 |
| `/srv/newspaper` | 当前静态网站 |

```bash
sudo -u newspaper /opt/Newspaper/.venv/bin/python -m newspaper.cli check-config
sudo -u newspaper /opt/Newspaper/.venv/bin/python -m newspaper.cli schedule
sudo -u newspaper /opt/Newspaper/.venv/bin/python -m newspaper.cli build
sudo systemctl enable --now newspaper.timer newspaper-clean.timer newspaper-notify.timer
sudo -u newspaper /opt/Newspaper/.venv/bin/python -m newspaper.cli status
```

命令在源码目录执行，或先将项目安装到虚拟环境。定时器每分钟检查任务，已发布时段直接跳过。单份失败至少等五分钟，六小时内最多三次失败尝试，不影响其他份继续运行。模型瞬时网络/HTTP 错误另有最多三次请求重试；systemd 单轮最长50分钟。推送重试、清理每五分钟运行，午夜及生成前也清理。所有写入任务共用进程锁。

将运行配置顶层 `base_url` 设为网站的 HTTPS 域名（如 `https://news.example.com`），推送阅读链接和 RSS 均使用此地址；它独立于 `[model].base_url`。修改后执行 `build` 更新 RSS，后续推送自动使用新地址。附带 Caddy 配置使用80端口 HTTP，请按部署配置域名、HTTPS、访问认证及防火墙。网站只暴露生成的公开文件，不暴露密钥、证据或管理接口。网站原子切换，构建失败保留上一完整版本；已复核的保存稿件恢复发布时不重新调用模型。

## 核验和保留期限

默认 bounded 模式通过固定版本 DSH 搜索插件完成检索和原文抓取，依次执行规划、初筛、取证、编辑、引文/日期/历史校验、独立模型复核。摘要只用于发现线索，不能作为发布证据；每项事实必须有匹配原文的引文，多源声明还需不同域名及语义独立性复核。限定次数修订后仅发布逐条通过的新闻。检索失败不能当作“无新闻”成功。自动核验仍不能保证事实绝对正确。

仅保留入选来源元数据、引文和相邻上下文。未入选网页、中间稿、DSH 会话/缓存在单次任务结束后删除，异常残留由清理任务回收。运行指标只留计数、结果和用量，不留响应正文或凭据。

保留发布当日及此前规则对应的30个日历日期，以项目时区计算；重建和重试不延长寿命。清理涵盖过期正文、证据、网页、下载、RSS、搜索索引、事件、推送队列、运行记录和旧网站版本，SQLite 使用安全删除并回收空间。新闻不进入 Git、不作为 CI 产物上传，本项目不备份新闻。读者下载和第三方服务已保存的副本不受服务器删除控制。

`make check test` 使用合成资料和模拟发送，离线检查日历、任务合并、防重复、重启恢复、筛选、证据、首页、清理与渲染，不发送真实邮件/TG。保留有用回归测试，仅清理临时探针和缓存。依赖固定在两个 lock 文件中，升级时同步检查 DSH 配置。

MIT 许可证。DSH 与插件保留各自许可证。
