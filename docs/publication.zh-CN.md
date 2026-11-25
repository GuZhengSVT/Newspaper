# 网站发布与推送

`build` 生成可读的完整站点，并以一次符号链接切换更新 `web_dir/current`。站点根目录包含 `site-manifest.json`（文件路径及 SHA-256）和 `site-version.txt`（清单字节的 SHA-256）。相同内容生成相同版本。`status` 的 `publication` 给出本地版本、确认的公网版本和当前状态。

## Caddy 直供

使用 `[publication] mode = "local"`，将 Caddy 的站点根目录指向 `web_dir/current`，将顶层 `base_url` 设为读者实际打开的根域名。程序以本地站点的原子切换作为投递条件。该模式适合 VPS 直接对外服务的安装。

## GitHub Pages

Pages 的构建任务从 HTTPS 源站下载本次清单中的文件，逐文件验证摘要，再部署整站产物。需要两处独立地址：源站由 Caddy 提供当前公开目录；读者使用绑定到 Pages 的域名。源站只开放生成的静态目录。模板采用从网站根目录开始的链接，Pages 应使用自定义根域名或账户根站点。

1. 将本项目的 `deploy/github/publish-pages.yml` 安装到目标仓库默认分支的 `.github/workflows/publish-pages.yml`，并加入 `scripts/fetch-pages-artifact.py`。在仓库的 Pages 设置中选择 GitHub Actions，在 Actions 仓库变量中设置 `NEWSPAPER_ORIGIN`，值为源站的完整 HTTPS origin。
2. 在 VPS 的私有 `credentials.env` 配置 `NEWSPAPER_PAGES_TOKEN`。凭据需要能够对该仓库派发 Actions 工作流。令牌只随请求发送给 GitHub API。
3. 在运行配置中填写读者域名和目标仓库。若部署分支不同，填写 `ref`，并确保相应分支包含上述两个文件。

   ```toml
   base_url = "https://news.example.com"

   [publication]
   mode = "pages"
   repository = "owner/site"
   ref = "main"
   ```

4. 从 Git 分支同步方案迁移时，安排切换窗口，停用 `newspaper-pages.timer`（旧安装使用 `newspaper-pages.path`）和分支同步服务，再按新的发布包安装步骤启用 `newspaper-publish.timer`。首次升级先执行 `build` 生成清单。也可以使用 `publish-site` 单独检查或触发发布。
5. Pages 工作流把内容版本和本次请求编号写入 `site-deployment.json`。VPS 在读者域名读回两者才确认成功；即使更正后恢复了旧内容，也会等对应的发布完成。下一轮推送只发送与已确认的公开 JSON 一致的刊物。域名尚未生效、工作流失败或取回文件时源站版本变化，都可以等下一轮继续，无需重新采编。工作流已派发后，程序按分钟检查公网，15 分钟仍未确认可重新派发。相同且已确认的版本在本地直接返回。

工作流将上传产物保留期设为一天。新闻下架由包含删除结果的新部署生效，远端不可用期间可能仍显示上一次成功版本。既有仓库的提交历史由站点所有者管理。有关部署环境和所需的工作流权限，参见 [GitHub Pages 自定义工作流](https://docs.github.com/en/pages/getting-started-with-github-pages/using-custom-workflows-with-github-pages)。

## 从已有的 Pages 分支发布

对于已经由 GitHub Pages 从分支根目录发布的站点，可保留该分支和自定义域名。在运行配置中填写读者使用的 HTTPS `base_url`，将 `publication.mode` 设为 `"pages_branch"`，填写站点仓库 `repository` 和分支 `ref`。

发布机需要 Git、rsync 和站点仓库的推送权限。将已有站点仓库根目录设为 `newspaper-pages.service` 的工作目录，运行 `/安装目录/.venv/bin/python /安装目录/scripts/sync-pages-branch.py --branch main --daily`，并启用 `deploy/newspaper-pages.timer`。旧安装需停用 `newspaper-pages.path`。

通过 systemd drop-in 将 `newspaper.service` 的生产命令设为 `python -m newspaper.cli run --daily-clean`，并停用独立的 `newspaper-clean.timer`。生产任务从项目时区的 12:00 开始，在同一把任务锁内先完成当日清理，再处理排定的刊物。可将 `newspaper-translate.timer` 的运行时段也从 12:00 开始。分支同步等待清理、当天所有排定的刊物及其待办翻译完成，再一次性推送站点。同步会保留仓库的 CNAME、README 和 Pages 设置，并随提交写入版本回执。

启用 `newspaper-publish.timer`，由程序从读者域名读取这次发布的回执。通知任务会等待回执，并核对即将发送的刊物与已公布的内容摘要。暂停通知期间通过 `run --no-notify` 生产的刊物仍然跳过投递。

## 排查

| 现象 | 查看 |
| --- | --- |
| `unbuilt` | 首次建站、渲染错误或清理时的过期下架。检查 `build` 或清理服务的日志。 |
| `awaiting_publication` | 对比 `status` 和读者域名的 `site-version.txt`；检查 `newspaper-publish.service`、仓库 Actions 以及源站地址。 |
| `live` 但双语订阅仍在等待 | 检查该账号实际订阅的子板块。选择中文先发时，对应文章全部有译文且该版已经发布后才会投递。 |
| 公网版本正确，某一收件人仍无消息 | 查看 `status` 的投递状态、渠道开关和该账号绑定。结果不确定的投递继续沿用人工核实规则。 |
