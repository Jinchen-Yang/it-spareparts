# PARTFLOW 员工连接器

`partflow/` 是按 WorkBuddy 开放平台格式准备的私有连接器源码，含5个 Skills、远程 Streamable HTTP 配置和个人令牌表单。没有发布到公共市场。

先在公司业务系统的 `/mcp-office` 登录独立员工账号，生成个人令牌。WorkBuddy 配置填写公司系统 HTTPS 根地址（不要填写营销演示站）和令牌，不把令牌发进对话。到期或撤销后重新生成并更新连接设置。

连接器包要求 WorkBuddy 4.24.0 及以上。格式依据 [WorkBuddy 官方连接器文档](https://open.workbuddy.cn/en/docs/connector)；已做静态契约检查和官方 Python MCP 客户端测试，尚未在员工的 WorkBuddy 桌面客户端实测。

本地先通过 WorkBuddy 的 MCP 配置入口使用 `partflow/mcp.json`，将占位符在配置编辑器内换为本人的实际配置；用户配置中的真实令牌不要提交版本库。Skills 按员工客户端的本地 Skills 安装入口加载。开放平台连接器发布需要独立上传审核，不是复制本目录即自动发布。

首版：网页上传下载＋后台预览/导出任务＋员工网页确认。Skills 可指导本地复制归档；尚未提供跨平台静默传文件。核心正式回传限于验收清单、费用回款工作簿；其他采购销售单据只预检。
