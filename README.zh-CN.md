# Newspaper

一个可独立部署在 VPS 上的每日新闻发布项目：DSH 按需检索和补查 → 事实整理、去重及复核 → 标准 JSON → Markdown、静态网页与 RSS → 邮件和 Telegram 通知。

模型默认为 `d1api/deepseek-v4.1-flash`，检索及原文抓取由 `dsh-free-search` 插件提供。默认由 AI 规划查询，程序调用 DSH 插件的搜索和抓取接口，任务完成就退出；可选 headless 模式用于需要自由补查的部署，网站由 Caddy 单独提供服务。无需保持电脑、Codex 会话或 DSH 网页服务运行。

## 安装

需要 Linux、Python 3.11+、Node 22.13+，正式部署还需要 systemd 与 Caddy。

```bash
git clone https://github.com/GuZhengSVT/Newspaper.git
cd Newspaper
make install
make check test
```

依赖锁定在 `requirements.lock` 和 `package-lock.json`，安装不会自动跟随最新版。DSH 插件的预发布依赖兼容处理说明在 `.npmrc`；升级时必须运行离线配置组合检查和测试。

## 配置与运行

正式部署将源码放在 `/opt/Newspaper`，运行 `sudo bash scripts/install-vps.sh`，然后修改：

- `/etc/newspaper/config.toml`：访问网址、模型、搜索参数、板块和推送开关。
- `/etc/newspaper/credentials.env`：模型密钥、SMTP、收件地址、Bot Token 和 Chat ID。

以上两份配置均在 Git 仓库外。密钥文件权限为 `640`，只允许 root 和项目服务用户读取。配置变量名见 `.env.example`。SMTP 使用已有邮箱服务的授权码或密码，不在 VPS 上自建邮件服务器。

`editorial.example.toml` 提供科技与政治、ACGN 两份日报和 15 个板块，可将其追加到本地配置。也可自行定义 `[[editions]]` 的 `id/name/sections` 与 `[[sections]]` 的 `id/name/scope/batch`。每个启用板块必须恰好属于一份日报；默认每板块规划两组中外文查询，数量由程序限制；可选 headless 模式下同一 batch 的相关板块合并检索。安装默认配置仍不预选内容，空配置不调用模型或推送。

```bash
sudo systemctl enable --now newspaper.timer newspaper-clean.timer newspaper-notify.timer
sudo systemctl list-timers 'newspaper*'
```

生成任务在每天北京时间 **12:00 开始**，通过复核后才发布和推送。清理任务在午夜运行，并每五分钟补检；推送重试每五分钟运行，已确认成功的通知不重复发送。远端请求超时可能造成发送结果不明确，不能保证所有异常情况下都绝对无重复。

网页为只读静态站点，支持移动端布局、明暗主题、字号切换、归档搜索、Markdown/JSON 下载与 RSS。当前部署模板使用 IP 的 HTTP 访问；有域名后可以改为 HTTPS。若需要密码保护，应与 HTTPS 一起配置。

## 内容与发布

`prompts/editorial.md` 是总编辑规则；其余提示词分别负责采集、候选筛选、编辑和复核。只发布“重大/重要”新闻，可靠性标记 A/B，C 类传闻排除；空板块在网页和 Markdown 中隐藏。基础学科提供面向普通本科生的详细解释，政治报道保持事实中性，新番和社区话题必须注明评价来源及样本限制。

`prompts/` 下保存各阶段提示词，`schemas/report.schema.json` 定义标准日报。程序再次通过插件抓取选定来源，要求每条事实附有能在抓取原文中找到的引文，并检查报道窗口、历史重复和来源编号。模型复核负责语义、来源独立性和日期口径，不能保证新闻事实绝对正确。

同一任务共享规范化 URL 的抓取缓存，相同正文去重。先用搜索摘要筛选待核验候选，最多抓取每份24个来源，再发送相关原文片段给编辑；摘要不能作为发布证据；复核只读取成稿、引用及相邻上下文。复核后的内容修订只替换受影响的新闻，结构校验失败可完整重试一次。SQLite 用实体索引和稳定事件键检索相关历史，只向模型发送相关事件的精简事实，避免整月全文重传。逐条检查引文存在；声明多源互证时，每条核心事实需引用不同来源域名，来源是否真正独立仍由证据复核判断。相同主体的重要后续需明确新增事实。

运行指标保存直接模型接口返回的 usage、各阶段输入字符数与耗时。默认 bounded 模式没有额外 DSH 模型调用，规划、筛选、编辑、复核用量统一记录；可选 headless 模式未返回可汇总用量时记录 null，不能把直接调用的 Token 误当成该模式整次任务总费用。

日报 JSON 是内容依据，Markdown 和网页由同一份数据构建。构建成功后原子切换网站版本，旧构建立即删除。生成或复核失败不会被当成成功发布，推送失败只重试通知，不重新生成日报。历史 Markdown 可用 `newspaper import-legacy` 导入，原文字节保留，网页注明为旧版格式。

## 30 天删除规则

保留当天与此前 29 天的日报，某期在其日期加 30 天的北京时间零点过期，重建和重试不会延长寿命。

抓取页面、未入选材料、候选计划、中间稿、DSH 会话及工具输出仅在任务临时目录存在，结束即删除。仅保留入选新闻的 `evidence.json`（来源元数据、引文和短上下文）用于追溯，不保存完整网页或模型思考过程。

清理覆盖证据资料、JSON、Markdown、网页、下载文件、RSS、搜索索引、旧构建、事件去重记录、推送记录及过期运行记录。DSH 的 HOME、会话、缓存和诊断输出置于单次任务临时目录，结束后删除；异常残留在下一次清理时删除。数据库启用 `secure_delete` 并整理回收空间。

**新闻数据不进入 Git，不上传到 CI，不由本项目备份。** 如 VPS 的整机快照或其他备份也受该要求约束，应单独排除数据与网站目录。已经被读者下载、缓存或由第三方服务留存的副本不受 VPS 控制，所以邮件和 Telegram 默认只发送日期及阅读链接，不附新闻全文。

## 目录

```text
/opt/Newspaper       源码、提示词、模板、Schema、测试
/etc/newspaper       本地配置与凭据，不入 Git
/var/lib/newspaper   SQLite、分日期/日报类型的正文与证据，不入 Git
/srv/newspaper       当前网站构建，不入 Git
```

两份日报路径为 `/daily/YYYY-MM-DD/technology-politics/` 和 `/daily/YYYY-MM-DD/acgn/`，下载路径对应 `/downloads/YYYY-MM-DD/<类型>.md`（也支持 `.json`）。历史单份日报路径继续有效。

`/feed.xml` 为合并 RSS，`/feed-technology-politics.xml` 和 `/feed-acgn.xml` 可分别订阅。

预览命令（照常发布网页和文档，但不创建或发送邮箱/TG 通知）：

```bash
sudo -u newspaper /opt/Newspaper/.venv/bin/python -m newspaper.cli run --no-notify
```

同日同类型已经发布则跳过，不覆盖正文，也不补发预览通知。某份失败不会阻止另一份继续；整体返回失败状态供 systemd 识别。

详细运维、环境变量与测试说明见 [英文说明](README.md)。本项目采用 MIT 许可证；DSH 及其插件保留各自许可证。
