# PARTFLOW 开发测试部署（非生产）

2026-09-16 经用户明确授权，从 ybwznt 的生产库创建独立开发测试副本。

- 生产保持 `https://ybwznt.com` / `spareparts`，迁移版本 `e3a7b9c2d4f6`。
- 测试入口 `https://mcp-test.yabowei.xyz/mcp-office`，协议 `https://mcp-test.yabowei.xyz/mcp`。
- 测试目录 `/opt/partflow-mcp-test`；Compose 项目 `partflow-mcp-test`。
- 测试数据库/卷 `partflow_mcp_test_20260916` / `partflow_mcp_test_database_20260916`。
- 测试数据库迁移至 `a16b7c8d9e01`，与生产数据库处于不同 PostgreSQL 容器。
- 同时复制生产上传目录到独立 `raw/`；备份保存在 `backups/production-20260916.dump`，另存 SHA-256。
- 原生产账号在副本中全部停用、密码哈希随机替换、令牌版本递增；新建 `mcp_dev_test` 测试管理员。
- 测试签名密钥、数据库密码、业务登录密码及 MCP 令牌均独立生成；没有使用生产密钥/API key。
- `MCP_TEST_ENVIRONMENT=true`；网页横幅、能力响应、协议服务名 `PARTFLOW-TEST` 明示测试。
- `ENVIRONMENT=prod` 是启用严格安全校验，不代表业务数据库是生产库。
- 允许测试库网页确认写入；采购/销售通用单据仍只预检。外部 LLM/Vision 未配置。

## 网络与生命周期

四个独立测试容器：db、app、worker、gateway。app/worker/db 仅在 internal 测试网络；gateway 另连接独立 ingress 网络，唯一发布口为宿主回环 `127.0.0.1:18093`。数据库无发布端口。gateway 仅代理 MCP、工作台、登录及健康入口，其他原生业务路由 404。

复用目标机已有 `it-spareparts-frontend:release-0422c35` 镜像中的 Nginx，以专用配置覆盖路由，不发布镜像内的原业务前端。仅 internal 网络的 Docker 容器无法在本机有效发布端口，因此设置独立 gateway 网络出口，不改生产网络。

独立 Cloudflare 命名隧道 `partflow-mcp-test-20260916` 由 `partflow-mcp-test-tunnel.service` 管理，systemd enabled；应用容器 unless-stopped。子域名不会改变 ybwznt.com、yabowei.xyz 或账单入口。

运维命令在 `/opt/partflow-mcp-test` 执行：

```sh
docker compose -f compose.yaml ps
docker compose -f compose.yaml logs --tail 50 app worker
systemctl status partflow-mcp-test-tunnel
```

停用测试：先停独立隧道，再执行本目录 `docker compose -f compose.yaml stop`；保留备份、数据卷、文件与审计。禁止在生产目录执行本测试部署的 bootstrap，禁止把测试数据库连接串放入生产配置。

## 首次初始化

先创建只含本项目的独立目录与密钥 `.env`，启动 db；pg_restore 恢复到明确命名的测试数据库。显式运行 `alembic upgrade head` 和 `alembic check`。`bootstrap_test.py` 验证环境标志、域名及实际数据库名，只能初次运行；使用只读挂载 `/run/test-account.json`，不会输出密码。

测试账号文件和连接配置保存在受控私有目录，不纳入 Git、公共静态站或连接器 ZIP。需要恢复历史业务文件时仅操作测试 `raw/`，不能挂载生产目录。测试副本仍含真实业务数据，不能当作脱敏公开演示数据发布。

## 已验收与边界

`acceptance.py` 通过公网 HTTPS 使用官方 MCP SDK：初始化、16工具、测试身份与快照标签、备份 PN/库存检索、上传、worker预览、专用 TEST 项目网页确认、导出、受保护下载、SHA-256、下载完成审计、撤销令牌拒绝。专用验收项目 `MCP-TEST-CANARY-20260916`。

真实 Chromium 公网登录、桌面/手机宽度、测试横幅、无脚本错误通过。22个 MCP 单元/集成用例通过（21个原有用例加1个测试标签及转义用例分别执行）。生产数据库未做 MCP 迁移、没有测试账号/验收项目。

MacBook 上真实 WorkBuddy 连接尚待用户操作；官方 SDK 公网成功不能替代该客户端验收。个人测试 MCP 令牌有效7天，到期可登录测试工作台重新生成。不要把连接令牌贴入聊天。
