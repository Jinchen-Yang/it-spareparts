# Architecture Decision Records

> 重大技术决策记录。格式：ADR-NNN，按时间倒序。

---

## ADR-001: 维保 Beta 总闸机制

**Date:** 2026-08-09

**Problem:** 维保模块功能多、风险高，需要在生产环境完全关闭但保留代码，同时允许特定白名单用户试用。

**Options Considered:**

- Option A: Git 分支隔离（功能在独立分支，部署时切换）
- Option B: 功能开关 + 白名单（运行时控制）

**Decision:** Option B — 功能开关 + 白名单

**Reason:**
- 避免长期维护独立分支的合并冲突
- 可以细粒度控制（总闸 + 角色 + 用户级白名单）
- CI 可以全量测试（测试环境开总闸）
- 生产零风险（总闸关 = 路由 404/403，代码路径不可达）

**Consequences:**
- 每个 Beta 路由需要守卫检查
- 测试必须覆盖"总闸关"场景
- `SystemSettings` 表增加 `maintenance_beta_enabled`, `replenishment_beta_enabled` 字段
- Beta 白名单表 (`maintenance_beta_allowlist`) 独立管理

---

## ADR-002: Alembic 单 Head 线性历史策略

**Date:** 2026-07-30

**Problem:** 多个 codex 分支并行开发维保子模块（#204/#205/#206/#207/#208），每个都有自己的迁移，直接合并会产生多个 head。

**Options Considered:**

- Option A: 允许多 head（每个分支独立迁移链）
- Option B: 强制 merge migration 保持单 head

**Decision:** Option B — 强制单 head

**Reason:**
- 多 head 在生产部署时容易出错（不知道执行哪个）
- 单 head 让 `alembic upgrade head` 每次都确定
- merge migration 可以控制分支合入顺序
- CI 中 `alembic check` 可以自动检测漂移

**Consequences:**
- 合并 codex 分支时必须创建 merge migration（已创建 7 个）
- Durable #234 因双 head 冲突被冻结（从同一 revision 分叉）
- 未来 AI 管线（Capability→Artifact→Durable）必须线性重建

---

## ADR-003: AI Chat 使用通用 Tool-calling Loop 而非专用 Pipeline

**Date:** 2026-07-15

**Problem:** 需要为备件系统引入 AI 助手，但不确定最终形态（简单问答 vs 多步骤工作流）。

**Options Considered:**

- Option A: 先建通用 Tool-calling Loop（OpenAI function calling 格式）
- Option B: 直接建专用 Pipeline（LangGraph / 定制工作流）

**Decision:** Option A — 通用 Tool-calling Loop

**Reason:**
- 快速上线验证价值（P0-P1 已完成）
- OpenAI function calling 是行业标准，多厂商兼容
- 不需要引入 LangGraph 等重型依赖
- 可以逐步演进到专用 Pipeline（P3-P4）

**Consequences:**
- 当前 agent/ 模块是通用 chat 引擎，不是专用 pipeline
- 工具和 skill playbook 可以独立扩展
- P3（Capability/Artifact/Broker/Durable）需要独立设计和重建
- 两条线可以并行开发，最终在合适的时机合并

---

## ADR-004: 数据导入采用 ETL Pipeline 模式

**Date:** 2026-06-20

**Problem:** Excel 数据导入涉及字段映射、清洗、校验、入库多个步骤，需要可测试、可扩展。

**Options Considered:**

- Option A: 单体 import 函数
- Option B: 独立 ETL Pipeline（reader → cleaner → transformer → loader）

**Decision:** Option B — ETL Pipeline

**Reason:**
- 每阶段可独立测试
- 新增导入模板只需加 transform 规则
- 预检 (precheck) 可以复用 pipe 前几段
- 清洗规则可配置（classify rules）

**Consequences:**
- `app/etl/` 目录：reader, cleaner, transform, loader, precheck
- 每个阶段有独立测试
- 导入错误按阶段追溯

---

## ADR-005: 前端状态管理不引入 Redux/Zustand

**Date:** 2026-06-10

**Problem:** 前端状态管理方案选择。

**Decision:** React Context + localStorage，不引入 Redux/Zustand

**Reason:**
- 当前状态简单（token, permissions, beta_features, tax_basis）
- 页面间状态独立，不需要全局 store
- 减少依赖和维护成本
- 未来如果真的需要（跨页面复杂状态），再评估引入

**Consequences:**
- 状态分散在 Context 和 localStorage 中
- 跨页面通信通过 URL params 或 localStorage event
- Token 变更通过 `window.addEventListener('storage')` 跨 Tab 同步
