# Technology Stack

> 最后更新：2026-08-10

## Frontend

| 类别 | 选择 | 版本 | 原因 |
|---|---|---|---|
| 框架 | React | 18.3 | 成熟生态，AntD 官方支持 |
| 语言 | TypeScript | 5.6 | 类型安全，项目已全量 TS |
| 构建工具 | Vite | 7.3 | 极快 HMR，Rollup 生产构建 |
| UI 库 | Ant Design | 5.21 | 企业级中后台首选，中文友好 |
| 图表 | ECharts | 6.1 | 国产，功能全面，备件分析报表 |
| 路由 | React Router | 7.18 | 标准选择 |
| HTTP | Axios | 1.18 | 拦截器、token 注入 |
| Markdown | react-markdown + remark-gfm | 10.1 | AI Chat 渲染 |
| 测试 | Vitest + Testing Library | 3.2 / 16.3 | Vite 生态，速度快 |
| 拖拽 | react-resizable | 3.2 | 工作台布局 |

**状态管理：** React Context（TaxBasis、BetaFeatures）+ localStorage（token/permissions/beta_features），未引入 Redux/Zustand（当前复杂度不需要）

**前端路由（19 个页面）：** Login, Accounts, MasterData, PartSearch, Import, Inventory, Pools, PoolAnalysis, Profit, Purchases, BossBoard, Chat, Governance, SystemSettings, ProjectCost, ProjectDownloads, ProjectReminders, ReplenishmentBeta, MaintenanceProjectMaster → 加上 15 个 maintenance/ 子页面

## Backend

| 类别 | 选择 | 版本 | 原因 |
|---|---|---|---|
| 框架 | FastAPI | 0.136 | 高性能异步，Pydantic 原生集成 |
| ASGI | Uvicorn | 0.47 | FastAPI 官方推荐 |
| ORM | SQLAlchemy | 2.0 | 成熟，Alembic 迁移生态 |
| 迁移 | Alembic | 1.13+ | SQLAlchemy 官方迁移工具 |
| 数据库 | PostgreSQL | 15 | JSONB 支持（维保字段弹性），性能好 |
| 驱动 | psycopg[binary] | 3.2 | PostgreSQL Python 驱动 |
| 数据校验 | Pydantic | 2.9 | FastAPI 核心依赖 |
| 配置管理 | pydantic-settings | 2.14 | 环境变量自动加载 |
| 数据处理 | Pandas + openpyxl | 2.2 / 3.1 | Excel 导入导出 |
| 文档解析 | python-docx + pdfplumber | 1.2 / 0.11 | AI 文件上传 OCR |
| AI SDK | openai | 1.0 | 对接 DeepSeek/通义等 OpenAI 兼容端点 |
| 依赖管理 | uv | 0.12 | Rust 实现，比 pip 快 10-100x |
| 测试 | pytest + httpx | 8.3 / 0.27 | 标准选择 |

**包管理：** setuptools + pyproject.toml（`[project]` 声明依赖，uv sync 锁定）

**后端模块（34 个 API 路由 + 63 个 Service）：**
- `app/agent/` — AI Chat 引擎（runtime, provider, tools, prompts, skills）
- `app/api/` — REST 路由（accounts, parts, purchases, maintenance*, agent, chat, replenishment 等）
- `app/services/` — 业务逻辑（maintenance_* 23 个, agent_files, chat_store, cost, classify 等）
- `app/models/` — ORM 模型（20 个文件）
- `app/etl/` — 数据导入流水线（reader, cleaner, transform, loader, precheck）

## Infrastructure

| 类别 | 选择 | 版本 | 原因 |
|---|---|---|---|
| 容器化 | Docker + Compose v2 | 29.1 / 2.40 | 开发生产一致 |
| CI/CD | GitHub Actions | — | 仓库原生集成 |
| 反向代理 | Caddy | — | 自动 HTTPS，配置简洁 |
| 日志 | Docker json-file | — | 简单可靠，限制 max-size/max-file |
| 定时任务 | cron (on host) | — | 备份、监控探针 |
| 网络 | Tailscale | — | 安全远程访问，不暴露公网端口 |
| 同步 | Syncthing | — | .codex 上下文多设备同步 |

## AI / Agent

| 类别 | 选择 | 原因 |
|---|---|---|
| LLM 提供商 | DeepSeek（默认）/ 通义 Qwen / 任意 OpenAI 兼容 | 国产，大陆直连，成本低 |
| Vision 模型 | Qwen-VL（通义 DashScope） | 图片/扫描件 OCR 识别 |
| Agent 架构 | Tool-calling Loop（OpenAI function calling 格式） | 标准协议，多厂商兼容 |
| Chat 持久化 | PostgreSQL（chat_session + chat_message 表） | 复用现有 DB |
| 上下文缓存 | DeepSeek 磁盘级自动缓存（system+tools 前置） | 零额外代码 |

**AI 演进路线（见 `docs/智能体平台架构与演进路线.md`）：**
- P0 ✅ 备件知识库 + 基础对话
- P1 ✅ 10 工具 + 4 Skill Playbook
- P2 ❌ RAG 知识库检索增强
- P3 ❌ 工作流编排（Capability → Artifact → Broker → Durable）
- P4 ❌ 插件化（Skill/Tool 热插拔）

## 开发环境

| 类别 | 选择 | 说明 |
|---|---|---|
| OS | Ubuntu 26.04 LTS (WSL) | Linux 原生，与生产一致 |
| Shell | Bash | 脚本和部署 |
| Git | 2.53 | `main` 保护分支，squash-merge |
| AI Coding | Claude Code 2.1.226 + OpenCode 1.18 | Node.js npm 全局安装 |
| Node 管理 | nvm | 版本灵活切换 |
| Python 管理 | uv | 统一依赖管理 |
