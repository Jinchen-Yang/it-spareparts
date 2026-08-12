# System Architecture

> 最后更新：2026-08-10

## System Overview

```
┌─────────────────────────────────────────────────────┐
│                    Caddy (HTTPS)                     │
│              reverse proxy :80/:443                  │
└─────────────────────┬───────────────────────────────┘
                      │
┌─────────────────────▼───────────────────────────────┐
│                 Frontend (React 18)                  │
│  AntD 5 · Vite 7 · TypeScript · React Router        │
│  Pages: 19 个主页面 + 15 maintenance 子页面          │
│  State: Context (TaxBasis, BetaFeatures) + localStorage│
└─────────────────────┬───────────────────────────────┘
                      │ HTTP/REST (Axios)
┌─────────────────────▼───────────────────────────────┐
│              API Layer (FastAPI 0.136)               │
│  34 routers · JWT Auth · RBAC · Pydantic validation  │
│  /api/accounts  /api/parts  /api/maintenance/*       │
│  /api/agent/chat /api/replenishment ...              │
└─────────────────────┬───────────────────────────────┘
                      │
┌─────────────────────▼───────────────────────────────┐
│          Business Services (63 services)             │
│  maintenance_* (23) · agent_files · cost · classify  │
│  import pipeline · chat_store · replenishment       │
└─────────────────────┬───────────────────────────────┘
                      │
┌─────────────────────▼───────────────────────────────┐
│              ORM (SQLAlchemy 2.0)                    │
│              Models (20 model files)                 │
│  Alembic Migrations (62 scripts, single-head chain)  │
└─────────────────────┬───────────────────────────────┘
                      │
┌─────────────────────▼───────────────────────────────┐
│              PostgreSQL 15 (Docker)                  │
│  DB: spareparts / spareparts_test                   │
└─────────────────────────────────────────────────────┘
```

## Module Structure

### Frontend (`frontend/src/`)

```
pages/
├── LoginPage.tsx              → 登录（唯一无需鉴权的页面）
├── MasterDataPage.tsx         → 备件主数据（型号 CRUD、搜索）
├── PartSearchPage.tsx         → 型号全局搜索
├── ImportPage.tsx             → 数据导入（Excel 预检 + 提交）
├── InventoryPage.tsx          → 库存管理
├── PoolsPage.tsx              → 互通池列表
├── PoolAnalysisPage.tsx       → 互通池分析
├── PoolManagementPage.tsx     → 互通池管理
├── ProfitPage.tsx             → 利润分析（双税）
├── Purchases/                 → 采购分析/异常/记录
├── BossBoardPage.tsx          → 老板看板
├── AccountsPage.tsx           → 账号管理（含权限中心）
├── ChatPage.tsx               → AI 助手对话
├── GovernancePage.tsx         → 数据治理
├── SystemSettingsPage.tsx     → 系统设置（含 Beta 开关）
├── ReplenishmentBetaPage.tsx  → 补库购物车 Beta
├── MaintenanceProjectMasterPage.tsx → 维保项目主档入口
├── ProjectCostPage.tsx        → 项目成本
├── ProjectDownloadsPage.tsx   → 项目下载
├── ProjectRemindersPage.tsx   → 项目提醒
└── maintenance/               → 15 个维保子页面
    ├── MaintenanceProjectWorkspacePage  → 项目工作台（四表）
    ├── MaintenanceProjectsPage         → 项目列表
    ├── MaintenanceManagerWorkbookPage  → 月报工作簿 v3
    ├── MaintenanceAcceptancePage       → 验收报告
    ├── MaintenanceWarehouseWorkbenchPage → 现场领用
    ├── MaintenanceDemandManagementPage → WBDD 安全删除
    ├── MaintenanceMigrationPage        → 成本迁移
    ├── MaintenanceSourceOrderAssignmentsPage → 来源单归属
    └── ...
```

### Backend (`backend/app/`)

```
app/
├── main.py                   → FastAPI 应用入口，注册 28 个 router
├── config.py                 → pydantic-settings 配置加载
├── db.py                     → SQLAlchemy engine + session
├── auth.py                   → JWT 鉴权（token 签发/验证/角色）
├── security.py               → 权限系统（UserContext, require_page, RBAC）
├── permissions.py            → 权限矩阵定义
├── beta_access.py            → Beta 白名单控制
├── maintenance_beta.py       → 维保 Beta 门控
├── tax_policy.py             → 双税策略
├── http_controls.py          → HTTP 安全头
├── seed_users.py             → 初始账号
│
├── agent/                    → AI 引擎
│   ├── runtime.py            → Tool-calling loop
│   ├── provider.py           → LLM 客户端抽象（DeepSeek/通义）
│   ├── tools.py              → 10 个 Agent 工具定义
│   ├── prompts.py            → System prompt + 6 场景
│   └── skills.py             → 4 个 Skill Playbook
│
├── api/                      → REST 路由 (34 文件)
│   ├── agent.py              → /api/agent/chat
│   ├── accounts.py           → 账号 CRUD + 权限模板
│   ├── maintenance.py        → 维保聚合入口
│   ├── maintenance_*.py      → 12 个维保子路由
│   ├── replenishment.py      → 补库 Beta
│   ├── purchases.py          → 采购
│   ├── imports.py            → 数据导入
│   ├── parts.py              → 备件型号
│   ├── dashboard.py          → 看板 KPI
│   └── ...
│
├── services/                 → 业务逻辑 (63 文件)
│   ├── maintenance_*.py      → 23 个维保服务
│   ├── agent_files.py        → Agent 文件处理
│   ├── chat_store.py         → Chat 持久化
│   ├── cost.py               → 成本计算
│   ├── classify.py           → 型号分类
│   ├── replenishment.py      → 补库审核
│   └── ...
│
├── models/                   → ORM 模型 (20 文件)
│   ├── maintenance.py        → 维保核心模型
│   ├── maintenance_bad_return.py → 坏件返还
│   ├── maintenance_project.py → 项目主档
│   ├── replenishment.py      → 补库模型
│   ├── chat.py               → AI Chat 模型
│   └── ...
│
├── etl/                      → 数据导入流水线
│   ├── reader.py             → Excel 解析
│   ├── cleaner.py            → 数据清洗
│   ├── transform.py          → 字段映射
│   ├── loader.py             → 入库
│   └── precheck.py           → 预检
│
└── schemas/                  → Pydantic schemas
```

## Data Flow

**典型用户请求（以"维保项目工作台加载"为例）：**

```
1. Frontend: MaintenanceProjectWorkspacePage 加载
   → GET /api/maintenance/projects/{id}/workspace
   → Header: Authorization: Bearer <token>

2. API Layer: maintenance_project_operations.router
   → Depends(get_current_user_context) → 解析 JWT，获取角色
   → Depends(require_page("page_maintenance_beta")) → 页面准入
   → Pydantic 校验 project_id

3. Service Layer: maintenance_project_operations.get_workspace()
   → 查询项目主档 (maintenance_projects)
   → 查询合同列表 (maintenance_contracts)
   → 查询回款记录 (maintenance_payments)
   → 查询领用记录 (maintenance_consumptions)
   → 查询报销记录 (maintenance_expenses)
   → 聚合四表 + 计算成本
   → 取价链冻结证据（maintenance_cost_reference）

4. ORM: SQLAlchemy session 执行参数化查询

5. DB: PostgreSQL 返回结果集

6. 响应：Pydantic model 序列化 → JSON → Frontend
```

**AI Chat 请求流：**

```
1. Frontend: ChatPage 发送消息
   → POST /api/agent/chat
   → { "messages": [...] }

2. API Layer: agent.router
   → 检查 enable_agent 总闸
   → 检查 page_chat 权限
   → record_access_log

3. agent/runtime.py: agent loop
   → 构建 system prompt（含角色上下文）
   → 调 LLM（DeepSeek/通义）
   → LLM 返回 tool_call → 调 tools.py 对应函数
   → 结果回传 LLM → 生成最终回答
   → 最多迭代 N 轮（llm_max_tool_iters）

4. 响应: { "answer": "...", "tool_calls": [...] }
```

## Architecture Rules

**分层规则（不得违反）：**

| 层 | 可以调 | 禁止调 |
|---|---|---|
| API (Router) | Service, Schema | Model (直接), 外部 API |
| Service | Model, 其他 Service | Router, HTTP 对象 |
| Model | DB Session | Router, Service, HTTP |
| Agent | Service, Provider | 直接的数据库写入 |

**Beta 功能规则：**
- 所有 Beta 路由必须经过 `require_maintenance_beta()` 或 `replenishment_beta_whitelisted()` 守卫
- Beta 功能默认关闭（`maintenance_beta_enabled=false`）
- 开启 Beta 需要：总闸 + 用户白名单 + 角色校验

**迁移规则（Alembic）：**
- 所有 schema 变更必须有迁移脚本
- 迁移链必须线性（单 head），合并时用 merge migration
- 迁移前在隔离测试库执行（CI `spareparts_test`）
- 禁止在迁移中执行数据变更（数据迁移单独走脚本）

**安全规则：**
- 生产：`ENVIRONMENT=prod` + 强密码/强密钥，默认弱值拒绝启动
- 开发：`ENVIRONMENT=dev` 可本地测试
- 不暴露应用端口到公网（只绑 `127.0.0.1`）
- 公网仅通过 Caddy 反向代理访问前端
