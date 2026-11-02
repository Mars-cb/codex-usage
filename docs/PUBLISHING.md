# 发布说明

## GitHub Release

使用 Actions → Release → Run workflow，或推送 `v<plugin.json版本>` 标签。只有测试通过后才会发布。标签已经指向其他提交时工作流会拒绝；不会强制移动标签或覆盖已有 Release。

发布产物：

- `codex-usage-vX.Y.Z.zip`：带市场清单的安装包，解压后可注册为本地插件市场。
- `codex-usage-panel-vX.Y.Z.zip`：独立插件包，根目录包含 `plugin.json` 与 `mcp.json`。
- `SHA256SUMS`：以上两个 ZIP 的校验值。

这些包是源码分发，不包含 Python、PyQt5 或 Codex 可执行文件。用户仍需满足运行依赖。

## 官方公开插件目录

官方公开目录与本仓库的 Git 市场不同。这个插件使用本机 stdio MCP，并启动本机图形程序，属于 Desktop only。

按照官方文档，本机 MCP 提交仅对获准合作方开放；提交组织需获准、具备提交权限并完成开发者或商业身份验证。确认这些条件后，在官方提交入口创建插件并上传独立插件 ZIP。上传、审核通过与正式发布是不同状态。

本仓库准备了清单、图标、示例截图、隐私说明和测试代码，但没有捆绑 Python/PyQt5。官方要求本机包附带服务及依赖；向官方提交前需要与其确认目标平台的依赖分发方式，并补齐该方式。审核所需演示视频、专用测试环境和发布者验证也需要由发布组织提供。

不应将本机 stdio 服务伪装成远程服务，或把 GitHub Release 误称为已上架官方目录。

官方依据：

- [本机插件提交资格和流程](https://developers.openai.com/plugins/deploy/desktop-only)
- [提交要求与发布入口](https://developers.openai.com/plugins/deploy/submission)
- [官方提交入口](https://platform.openai.com/plugins)
