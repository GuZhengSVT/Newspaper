# Newspaper

一个可独立部署在 VPS 上的每日新闻发布项目：DSH 按需检索和补查 → 事实整理、去重及复核 → 标准 JSON → Markdown、静态网页与 RSS → 邮件和 Telegram 通知。

模型默认为 `d1api/deepseek-v4.1-flash`，检索及原文抓取由 `dsh-free-search` 插件提供。DSH 使用一次性 headless 模式，任务完成就退出，网站由 Caddy 单独提供服务。无需保持电脑、Codex 会话或 DSH 网页服务运行。

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

板块无默认预选值。添加 `[[sections]]`，填写 `id`、`name`、`scope`、`keywords`，即可按自己的需求启用。板块为空时自动任务只清理和刷新网站，不调用模型、不伪造空日报、不发送日报通知。

```bash
sudo systemctl enable --now newspaper.timer newspaper-clean.timer newspaper-notify.timer
sudo systemctl list-timers 'newspaper*'
```

生成任务在每天北京时间 **12:00 开始**，通过复核后才发布和推送。清理任务在午夜运行，并每五分钟补检；推送重试每五分钟运行，已确认成功的通知不重复发送。远端请求超时可能造成发送结果不明确，不能保证所有异常情况下都绝对无重复。

网页为只读静态站点，支持移动端布局、明暗主题、字号切换、归档搜索、Markdown/JSON 下载与 RSS。当前部署模板使用 IP 的 HTTP 访问；有域名后可以改为 HTTPS。若需要密码保护，应与 HTTPS 一起配置。

## 内容与发布

`prompts/` 下分别是资料采集、编辑和复核提示词，`schemas/report.schema.json` 定义标准日报。程序再次通过插件抓取选定来源，要求每条事实附有能在抓取原文中找到的引文，并检查报道窗口、历史重复和来源编号。模型复核负责语义、来源独立性和日期口径，不能保证新闻事实绝对正确。

日报 JSON 是内容依据，Markdown 和网页由同一份数据构建。构建成功后原子切换网站版本，旧构建立即删除。生成或复核失败不会被当成成功发布，推送失败只重试通知，不重新生成日报。历史 Markdown 可用 `newspaper import-legacy` 导入，原文字节保留，网页注明为旧版格式。

## 30 天删除规则

保留当天与此前 29 天的日报，某期在其日期加 30 天的北京时间零点过期，重建和重试不会延长寿命。

清理覆盖原始资料、JSON、Markdown、网页、下载文件、RSS、搜索索引、旧构建、事件去重记录、推送记录及过期运行记录。DSH 的 HOME、会话、缓存和诊断输出置于单次任务临时目录，结束后删除；异常残留在下一次清理时删除。数据库启用 `secure_delete` 并整理回收空间。

**新闻数据不进入 Git，不上传到 CI，不由本项目备份。** 如 VPS 的整机快照或其他备份也受该要求约束，应单独排除数据与网站目录。已经被读者下载、缓存或由第三方服务留存的副本不受 VPS 控制，所以邮件和 Telegram 默认只发送日期及阅读链接，不附新闻全文。

## 目录

```text
/opt/Newspaper       源码、提示词、模板、Schema、测试
/etc/newspaper       本地配置与凭据，不入 Git
/var/lib/newspaper   SQLite、分日期的日报及原始资料，不入 Git
/srv/newspaper       当前网站构建，不入 Git
```

默认网站路径：`/` 首页，`/daily/YYYY-MM-DD/` 日报，`/downloads/YYYY-MM-DD.md` 文档，`/feed.xml` RSS。

详细运维、环境变量与测试说明见 [英文说明](README.md)。本项目采用 MIT 许可证；DSH 及其插件保留各自许可证。
