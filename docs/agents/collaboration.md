# 多 Agent 分层流水线与交接

## 1. 协作拓扑

```text
父 Feature Issue（完整业务验收、Contract SHA、依赖 DAG）
  ├─ Contract 子 Issue / PR（先冻结边界）
  ├─ Database 子 Issue / worktree / PR
  ├─ Backend 子 Issue / worktree / PR
  ├─ Frontend 子 Issue / worktree / PR
  └─ Integration 子 Issue（仅在需要代码或专项验证时）
          -> 各层独立 Review
          -> Integration Owner 按准确 SHA 组装
          -> migration -> Backend -> API -> Frontend tracer-bullet
          -> merge / deploy / user acceptance gates
```

父 Issue 不直接写代码。一个 Feature 可以并行运行多个子 Issue；唯一 Write Owner 约束作用于每个实现子 Issue，而不是整个 Feature。

Kimi 与 GLM 的 prompt cache 和 session memory 不共享；它们通过父子 Issue、Contract SHA、Draft PR、commit 和仓库文档共享项目事实。

## 2. 角色

| 角色 | 解决什么问题 | 上游依赖 | 下游消费 |
| --- | --- | --- | --- |
| Task Owner | 定义完整业务目标、非目标和验收 | 用户需求、正式业务口径 | 父 Issue、Contract Owner |
| Contract Owner | 把跨层边界冻结成可引用 SHA | 父 Issue 验收、数据规则 | DB/Backend/Frontend Agents |
| Workstream Write Owner | 在单个子 Issue 边界内实现一层 | 冻结 contract、依赖 SHA | 下游 Workstream、Reviewer |
| Reviewer | 基于准确 head SHA 审查单层实现 | 子 PR、契约与测试 | Write Owner、Integration Owner |
| Integration Owner | 管理 DAG、边界冲突和最终 tracer-bullet | 所有子 PR 与 Contract SHA | 合并、发布、用户验收 |
| Release Owner | 执行获授权的迁移、部署、回滚和生产核对 | 已集成 SHA、发布计划 | 生产状态与审计 |

同一个人或 Agent 可以承担多个角色，但同一实现子 Issue 同一时刻只能有一个 Write Owner。

## 3. Workstream 契约

| Workstream | 负责产出 | 上游依赖 | 下游消费 | 默认 Owned paths |
| --- | --- | --- | --- | --- |
| Contract | 字段、语义、示例、兼容性、Contract SHA | 父 Feature 验收 | DB、Backend、Frontend、Integration | `.ai/contracts/`、明确分配的 contract artifact |
| Database | 表、字段、约束、索引、迁移、ORM 边界 | 业务对象、DB contract | Backend | `backend/alembic/versions/`、`backend/app/models/` |
| Backend | Service、API、Pydantic/OpenAPI contract、错误语义 | DB contract 与 DB SHA | Frontend、Integration | `backend/app/services/`、`backend/app/api/`、`backend/app/schemas/` |
| Frontend | API 调用、手写 TS contract、页面和交互 | API contract 与 mock/Backend SHA | 用户验收、Integration | `frontend/src/api/`、`frontend/src/pages/`、`frontend/src/components/` |
| Integration | 跨层测试、SHA 组合与必要的胶水修复 | 所有上游 PR | 合并与发布 | 由父 Issue 逐项分配，不默认拥有产品目录 |

当前仓库没有已采用的 OpenAPI TypeScript codegen 流程，因此前端沿用 `frontend/src/api/` 内的手写类型。引入 codegen、生成目录或新锁文件属于新的架构决策，不能由单个 Frontend Agent 顺手完成。

## 4. 边界文件归属

最容易冲突的文件必须在子 Issue 中显式写进 `Owned paths` 或 `Forbidden paths`：

| 边界 | 默认 Owner | 规则 |
| --- | --- | --- |
| Alembic migration 与数据库约束 | Database | Backend/Frontend 不修改 |
| SQLAlchemy model 与 model registry | Database | 若影响公共注册，PR 中列出所有消费者 |
| Pydantic schema 与 OpenAPI 行为 | Backend | 必须实现冻结的 API contract |
| TypeScript API 类型 | Frontend | 必须注明消费的 Contract/Backend SHA |
| `.ai/contracts/` | Contract | 变更后重新冻结 Contract SHA |
| 公共配置、依赖锁文件、共享生成物 | Contract/Integration | 逐文件分配，不默认归任一层 |
| 跨层 E2E fixture / seed | Integration | 记录 DB/API/Frontend SHA 与数据版本 |

如果两个 Workstream 必须共同修改一个边界文件，先建立单独的 contract 或 integration 子 Issue。不要让两个 Agent 在各自 PR 中竞争修改同一文件。

## 5. 契约冻结

契约冻结解决“下游不必猜字段”的问题。

Contract Owner 的 PR 至少写明：

- 数据库表/字段/约束或迁移边界；
- API 请求、响应、空值、错误与权限语义；
- 至少一个真实形态的 request/response 示例；
- Frontend 所需字段和兼容策略；
- 旧数据、旧客户端、迁移、回填或 mock 规则；
- 受影响的 Workstream 子 Issue。

Integration Owner 接受后，在父 Issue 记录：

```text
Contract PR:
Contract SHA:
Frozen at:
Frozen by:
Applies to:
Compatibility:
```

Contract SHA 必须是远端可读取的完整 commit SHA，不能写“main 最新版”“某分支当前内容”或本地未提交状态。下游 Issue 的 `Consumes` 必须引用该 SHA。

契约变化使用新 commit 重新冻结，并显式更新受影响子 Issue 的 `Consumes`。如果下游已实现，父 Issue 还要记录重测范围和是否废弃旧 mock。

## 6. 依赖 DAG 与并行方式

推荐依赖关系：

```text
Contract
  ├─ Database -> Backend
  └─ Frontend mock
Database + Backend
  -> Frontend real-API verification
  -> Cross-layer integration
```

通常合并顺序：

```text
Contract PR -> Database PR -> Backend PR -> Frontend PR -> Integration gate
```

这不要求串行等待所有开发：

- Database Agent 可在 DB contract 冻结后实施；
- Backend Agent 可先基于冻结 contract 编写 service/API 测试，但真实运行验证依赖 DB SHA；
- Frontend Agent 可基于冻结 API 示例和 mock 并行开发，但最终必须切换真实 Backend；
- 下游每次验证都记录实际消费的 Contract、DB、Backend 或 Frontend SHA。

父 Issue 的 `Depends on` / `Blocks` 是依赖真相；Agent 不从聊天顺序推断依赖。

## 7. Worktree 与运行态隔离

- 每个实现子 Issue 使用独立 worktree 和 branch，从准确的远端 base SHA 创建。
- worktree 路径、branch、base SHA 和 Contract SHA 必须写进 Claim。
- 不进入另一个 Agent 或用户已有的 dirty worktree。
- Reviewer 默认只读作者 branch；需要修复时由原 Write Owner 处理。
- 各自开发环境使用明确端口、数据库和进程，不随机复用其他 Agent 的后台服务。

多个 Workstream 可以有意共享一个集成环境，但父 Issue 必须记录：

```text
Environment name / URL:
Integration branch or head:
DB migration/head:
Backend SHA:
Frontend SHA:
Contract SHA:
Snapshot/seed version:
Started/verified at:
Owner:
```

记录不得包含密码、token、连接串或生产敏感数据。环境中的任何层 SHA 变化后，旧验证结论自动失效，必须重新记录。

## 8. Claim 与失联回收

Claim 是子 Issue 的轻量协作记录，不是分布式锁。它只防止同一实现子 Issue 被两个 Write Owner 同时修改。

- Claim 必须包含 `Next checkpoint due`。
- 到期后，Task/Integration Owner 先检查 Issue、PR、远端 branch 和最后 SHA，再提醒原 Owner。
- 确认无人继续写后，发布可见事实 checkpoint 并标记 `Claim released as stale`。
- 新 Owner 从记录的 SHA 接手；找不到的未提交内容视为未知，不假定存在。
- 不自动删除 worktree、force-push、清理 branch 或覆盖本地文件。

## 9. PR、Review 与交接

每个子 PR 必须让接手者只靠仓库事实继续：

- 父/子 Issue 与 Workstream；
- base/head SHA；
- Consumes、Produces、Contract SHA；
- Owned/Forbidden paths；
- 已完成和明确未完成范围；
- 验证命令及结果；
- 上下游阻塞与解锁条件；
- 是否涉及迁移、回填、重导、发布或人工确认。

Reviewer 基于准确 head SHA 做只读审查。单层 PR 可合并只代表该子 Issue 达标；Integration Owner 仍需核对最终 SHA 组合。

## 10. 最终 Integration Gate

Integration Owner 在干净的集成 worktree/环境完成：

```text
真实迁移
  -> Backend 使用新 Schema
  -> API 返回冻结字段与语义
  -> Frontend 使用真实 API
  -> 一个端到端 tracer-bullet
```

最低证据：

- migration upgrade 结果与当前 head；
- Backend 测试和运行 SHA；
- API request/response 或契约测试；
- Frontend typecheck/build 和运行 SHA；
- 一个从输入/数据库事实到页面结果的 tracer-bullet；
- 失败时精确定位到哪个 Workstream 和哪个 SHA。

三个 PR 分别为绿色不能替代这道门。

## 11. Cache 与上下文控制

- 稳定、低频变化内容固定在 `AGENTS.md`、`CONTEXT.md`、ADR 和 contracts。
- 高频变化内容放在单个父/子 Issue 和 Draft PR；Agent 启动时只读取自己的子 Issue、父 Issue 摘要与直接依赖。
- 不把全量历史 Issue、完整 `.ai/CHANGELOG.md` 或另一 Agent 的 session 注入每次 prompt。
- 切换模型或 reasoning 配置时假定 provider cache 可能失效；项目连续性依靠 Contract SHA 和仓库事实。
- checkpoint 保持短、结构固定、带 SHA，以降低上下文成本并方便 Review。

## 12. 发布边界

本地测试、单层 CI、子 PR 可合并、Feature 集成通过、已合并、已部署和真实用户验收分别记录。合并、迁移、回填、生产写入、部署和重启必须由获授权的 Integration/Release Owner 执行。
