# 密钥与服务器安全交接

本仓库、GitHub Issue 与交接 ZIP **不保存**服务器密码、SSH 私钥、GitHub token、数据库密码、生产 `.env`、LLM/Vision API key 或真实客户附件地址。

## 应安全移交的配置项

| 配置类别 | 变量或位置 | 安全移交方式 |
|---|---|---|
| 生产数据库和应用 | `POSTGRES_PASSWORD`、`ADMIN_PASSWORD`、`SECRET_KEY` | 受控密码管理器共享；不得通过聊天、邮件、Issue 或 ZIP 发送 |
| 维保 manifest 签名 | `MAINTENANCE_MANIFEST_ACTIVE_KEY_ID`、`MAINTENANCE_MANIFEST_ACTIVE_HMAC_KEY`、历史 key JSON | 由发布负责人在受控密钥库移交；轮换必须保留审计 |
| AI 服务 | `LLM_API_KEY`、`VISION_API_KEY`、对应 Base URL/Model | 本机/服务器 `.env` 中单独配置；生产与开发使用不同 key |
| GitHub 与部署 | GitHub token、Deploy Key、服务器 SSH key | 为新电脑/新服务器单独生成最小权限 key，不复制旧私钥 |
| 域名与入口 | 正式域名、Caddy/Nginx/宝塔配置、旧域跳转配置 | 按 `docs/releases/https-ingress-runbook.md` 由运维在目标服务器复核 |

## 接手操作

1. 通过现有的受控密码管理器取得生产配置；若没有受控存放位置，先建立一个，再迁移。
2. 新电脑只创建开发 `.env`，使用本地随机值或受限测试 key；生产凭据不得落盘到普通工作区。
3. 为新设备创建单独 SSH/Deploy key，登记公钥，撤销不再使用的旧设备权限。
4. 此对话中曾出现过原始 API key；应立即在供应商后台轮换，并更新受控密钥库和服务器 `.env`，不要把旧值迁移到任何文档。
5. 发布前按版本 Runbook 重新从生产环境采集实时事实；文档中的历史结论不能替代实时核验。

## 本地开发所需的非秘密配置

- Python 3.11+、Node.js 20+、npm、Docker Engine + Docker Compose v2。
- `scripts/bootstrap-dev.sh --with-db` 安装代码依赖并启动本地 PostgreSQL。
- 根目录 `.env.example` 和 `backend/.env.example` 只提供变量结构，不能当作生产配置。
