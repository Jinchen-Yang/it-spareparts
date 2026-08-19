# Multi-Agent Development Workflow

> 本协议适用于 Kimi Code、ZCode、OpenCode、Codex、Claude Code 等开发 Agent。目标是让数据库、后端、前端和集成工作可以并行，又不依赖任何聊天历史猜测上下游契约。

## 1. 项目记忆的分层

| 层 | 权威载体 | 记录内容 |
| --- | --- | --- |
| 稳定规则 | `AGENTS.md` | 执行边界、数据链路、验证与发布要求 |
| 业务与决策 | `CONTEXT.md`、`docs/adr/`、模块文档、`.ai/contracts/` | 术语、架构、契约、长期决策 |
| Feature 聚合 | 父 GitHub Issue | 业务验收、Workstreams、依赖 DAG、Contract SHA、集成环境与最终状态 |
| 分层实现 | 子 GitHub Issue + Draft PR | 单层范围、Owner、Consumes/Produces、路径边界、diff 与验证 |
| 已发生事实 | commit、CI、部署与验收记录 | 可复核结果 |

`AGENTS.md` 是唯一通用入口；各 Agent 的专属配置只能做适配，不能维护第二份项目状态。

## 2. 建立 Feature 与依赖 DAG

一个跨层 Feature 先创建父 Issue。父 Issue 不直接写代码，由 Task/Integration Owner 维护：

- 完整业务目标、非目标和验收标准；
- 数据链路、表结构事实和备件影响；
- 契约冻结记录；
- Contract、Database、Backend、Frontend、Integration 子 Issue；
- `Depends on` / `Blocks` 依赖 DAG；
- 合并顺序、共享集成环境和最终验收。

每个实际实现层建立独立子 Issue。一个 Feature 可以并行运行多个子 Issue；“一个 Write Owner”只约束单个实现子 Issue。

## 3. 契约冻结

跨层并行的同步点是 Contract SHA，不是聊天约定。

1. Contract Owner 在专用 contract 子 Issue/PR 中定义数据库边界、API/Pydantic/OpenAPI contract、示例 payload、空值/错误语义和兼容性。
2. Integration Owner 审核后，在父 Issue 记录 Contract PR、准确 commit SHA、冻结时间和受影响子 Issue。
3. Database、Backend、Frontend 子 Issue 的 `Consumes` 必须引用这个 SHA；Frontend 可基于冻结 contract 使用 mock 并行开发。
4. 契约发生变化时，新建 commit 并重新冻结；父 Issue 和全部受影响子 Issue 同步更新，不允许下游继续依赖“最新版”或口头字段。

如果 SQLAlchemy model、Pydantic schema、TypeScript contract、公共配置或生成物的归属存在争议，先创建 contract 子 Issue，由 Contract/Integration Owner 分配边界。

## 4. 开始实现子 Issue

1. 确认父 Issue、Workstream、依赖和当前冻结的 Contract SHA。
2. 只读检查子 Issue Claim、开放 PR、远端 branch 和待修改路径。
3. 从准确的远端 base SHA 创建独立 branch 和独立 worktree。
4. 读取 `AGENTS.md`、`docs/agents/issue-tracker.md`、`docs/agents/collaboration.md`，再按任务读取相关业务文档、ADR、契约、源码、迁移和测试。
5. 在子 Issue 留下 Claim，写明执行器、模型、base SHA、branch、worktree、Consumes/Produces、Owned/Forbidden paths 和下一次 checkpoint 时间。
6. 按 `AGENTS.md` 输出短计划；仅高风险或有关键歧义时等待额外确认。

没有父/子 Issue 时可以先做只读诊断，但不得把未定义范围的诊断直接扩展为写入任务。

## 5. 分层开发

- Database Agent 默认负责 `backend/alembic/versions/`、`backend/app/models/`、数据库约束与迁移验证。
- Backend Agent 默认负责 `backend/app/services/`、`backend/app/api/`、`backend/app/schemas/` 和 API contract 的实现。
- Frontend Agent 默认负责 `frontend/src/api/`、`frontend/src/pages/`、`frontend/src/components/` 及当前项目的手写 TypeScript API 类型。
- Contract/Integration Owner 负责显式分配共享注册表、公共配置、锁文件、contract artifact 和跨层测试文件。
- 各 Agent 只能写子 Issue 的 `Owned paths`；需要越界时先更新 Issue 并由 Integration Owner 确认。
- 新发现的独立问题建立关联子 Issue，不偷偷扩大当前范围。
- 每个关键阶段都在子 Issue 或 Draft PR 留 checkpoint：准确 SHA、已完成、证据、未完成、阻塞和下一步。
- 本地通过不得写成 CI、可合并、已部署或已验收。

## 6. Draft PR 作为分层交接页

形成可审查 diff 后创建 Draft PR，并持续维护：

- 父 Feature Issue、实现子 Issue 和 Workstream；
- executor、base SHA、head SHA；
- `Consumes`、Contract PR/SHA、`Produces`；
- Owned/Forbidden paths 与明确未改内容；
- 数据链路、表结构事实和备件影响；
- 测试、构建、只读查询、mock 或截图证据；
- 迁移、回填、兼容性、依赖和下游解锁条件；
- 当前阻塞、下一步和 Review 重点。

单层 PR 的“可合并”只说明该子 Issue 达标，不代表父 Feature 已完成。

## 7. 合并与最终集成

通常顺序为：

```text
Contract PR
  -> Database PR
  -> Backend PR
  -> Frontend PR
  -> Cross-layer integration
```

Frontend 可在 contract 冻结后基于 mock 并行，但最终必须对真实 Backend 重新验证。若前置 PR 尚未合并，下游必须记录它实际消费的准确 SHA；禁止只写分支名或“最新版本”。

Integration Owner 在准确 SHA 组合上执行：

```text
真实迁移
  -> Backend 连接新 Schema
  -> API 返回冻结 contract
  -> Frontend 使用真实 API
  -> 一个端到端 tracer-bullet
```

共享集成环境允许被多个 Agent 有意复用，但父 Issue 必须记录环境标识、DB migration/head、Backend SHA、Frontend SHA、数据 snapshot/seed、验证时间和 Owner。各自开发环境不得随机复用其他 Agent 的端口、数据库或后台服务。

## 8. Claim 失联回收

Claim 不是永久锁。每个 Claim 包含 `Next checkpoint due`；逾期后由 Task/Integration Owner：

1. 只读检查 Issue、Draft PR、远端 branch 和最后可见 SHA；
2. 在 Issue 提醒原 Owner；
3. 确认无人继续写后，发布基于可见事实的 checkpoint，并标记 `Claim released as stale`；
4. 新 Owner 再从记录的 SHA Claim。

不得因 Claim 逾期自动删除 worktree、force-push、覆盖未提交文件或猜测隐藏进度。

## 9. 完成与发布边界

父 Feature 只有在以下事实均有证据时才可关闭：

- 所有必需子 Issue 的验收标准完成；
- 最终 Contract SHA 与实际 DB/Backend/Frontend SHA 一致；
- 跨层 tracer-bullet 通过；
- 需要的 PR 已合并；
- 迁移、回填、重导或发布动作已明确记录；
- 若要求生产生效，已记录 deployed SHA 和生产验证；
- 若要求真实用户验收，已单独记录验收结果。

提交、推送、创建或合并 PR、创建标签、数据库写入、迁移、部署和服务重启仍需按 `AGENTS.md` 取得授权。

## 10. 禁止模式

- 用一个实现 Issue 同时承载 DB、Backend、Frontend 三个 Write Owner；
- 让父 Feature Issue 直接挂多个 `agent:*` 标签或承担代码分支；
- 下游依赖未冻结字段、浮动分支名或聊天中的“最新版”；
- 两个写 Agent 共用同一 dirty working tree；
- 用全局 `.ai/CURRENT_STATE.md`、`.ai/CURRENT_TASK.md` 或 `TODO.md` 调度并发任务；
- 启动每个 session 时全量加载 `.ai/CHANGELOG.md`；
- 在 Issue、PR、日志或文档中写入密码、token、连接串或生产数据；
- 三个单层 PR 通过后跳过跨层集成，就声称 Feature 完成或可生产。
