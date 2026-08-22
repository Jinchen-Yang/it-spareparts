# IT_data 多 Agent 工作协议

本文件适用于整个仓库，也是 Kimi Code、ZCode、OpenCode、Codex、Claude Code 等开发 Agent 的共同项目入口。目标是在保持数据正确、业务口径一致和改动可验证的前提下，提高分析、开发和交付效率。

## 1. 系统主干：一切围绕备件流转

IT_data 是以数据为中心的备件管理系统。分析任何需求时，先回答：

1. 这项需求中的备件是什么，如何被唯一识别？
2. 备件从哪个事实源进入系统，经过哪些表、服务和接口？
3. 最终由哪个页面、报表、工作簿或业务动作消费？

备件身份的主轴是：

```text
Excel/业务事实源
  -> pn_raw / pn_std 解析与追溯
  -> dim_part.id（part_id，系统内稳定身份）
  -> 采购 / 销售 / 库存 / 维保 / 互通池 / 补库事实
  -> 服务计算
  -> API
  -> 前端页面、导出和业务决策
```

- 跨表关联、过滤和聚合优先使用 `part_id`。
- `pn_raw`、事实表中的 `pn_std` 主要用于来源追溯和展示，不得在已有 `part_id` 时重新充当跨域身份键。
- `dim_part.pn_std` 是备件主数据的标准 PN；别名、合并和改名必须继续保持到 `dim_part.id` 的稳定映射。
- 每个开发计划必须写明“备件影响”。如果确实与备件无关，也要用一句话说明原因。

## 2. 规划前的数据库事实检查

任何开发计划开始前，先定位并检查本任务涉及的数据库表、字段、主键、外键、唯一约束和关键索引。不要只根据页面字段、注释或旧文档猜测数据库设计。

事实优先级：

```text
当前数据库只读查询结果
  > 当前 Alembic 迁移链与 SQLAlchemy 模型
  > 当前 service/API 实现与测试
  > 当前业务文档和契约
  > 注释、旧计划和历史说明
```

执行要求：

- 数据库可连接时，优先用只读 SQL 或 SQLAlchemy inspection 查询 `information_schema.columns`、约束、索引及必要的少量数据分布。
- 不输出连接串、密码、token 或其他凭据。
- 数据库不可连接时，至少检查：
  - `backend/app/models/`
  - `backend/alembic/versions/`
  - 涉及该表的 service、API 和测试
- 无法核对运行中数据库时，必须写明“仅验证代码定义，未验证当前运行库”。
- 不需要为了一个任务扫描全部表，只查任务直接相关的表及其上下游关联表。

每份计划先给出一张最小数据表：

| 表 | 关键字段 | 在本需求中的职责 | 读/写 | 证据来源 |
| --- | --- | --- | --- | --- |

## 3. 先判断是哪条数据链路

规划时必须先把任务归入下面一种；不同链路使用不同思路，不得混在一起直接改页面或接口。

### A. Excel -> 数据库：采集与入库问题

适用于新增 Excel、表头变化、字段映射、导入失败、数据覆盖、幂等、原始值追溯等问题。

按以下顺序检查：

```text
真实 Excel 表头/样例
  -> reader / sheet_selection
  -> mapping
  -> cleaner / transform / precheck
  -> loader / upsert
  -> 数据库目标表、约束和审计
```

必须回答：

- Excel 是单表头、双表头，还是表头/明细混合？
- 原始字段、标准化字段和系统派生字段分别是什么？
- 备件 PN 如何解析成 `part_id`？原始 PN 如何保留？
- 唯一键和 upsert 覆盖规则是什么？重传是否幂等？
- 缺列、空值、重复行、表头版本变化如何处理？
- 写入哪些表，是否需要迁移、回填、审计或重算？

### B. 数据库 -> 处理层 -> 前端：读取与业务计算问题

适用于查询、聚合、成本、库存、看板、API、权限后展示和前端交互。

按以下顺序检查：

```text
数据库真实字段与数据状态
  -> service 查询、关联和业务计算
  -> API 路由与响应 schema
  -> frontend/src/api 请求和 TypeScript 类型
  -> 页面组件、格式化和用户动作
```

必须回答：

- 页面字段来自哪张表的哪个字段，还是服务层派生值？
- 查询通过什么键关联备件和其他业务对象？
- `null`、未导入、未知、受限和真实 `0` 是否被严格区分？
- 聚合口径、时间窗、税口径、状态过滤和排序在哪里定义？
- API 字段和前端 TypeScript 类型是否一致？
- 修改后哪些页面、导出、Agent 工具或其他接口会被连带影响？

### C. 跨链路：Excel -> 数据库 -> 处理层 -> 前端

如果字段从源 Excel 一直新增或改变到页面，必须按 A、B 两条链路完整检查，并补充：

- 数据库迁移与旧数据兼容；
- 历史批次是否需要回填或重导；
- 新旧 Excel 表头是否并存；
- API 是否需要兼容旧客户端；
- 前端在旧数据尚未补齐时如何诚实展示。

## 4. 高效工作方式

先判断用户是在要求“解释、诊断、修改还是发布”，然后按对应边界工作：

- 解释/审查：只读检查，直接给证据和结论，不修改文件。
- 诊断：定位原因和影响面；用户没有要求修复时，不自动实现。
- 修改/开发：给出短计划后直接完成实现和验证，不因普通多文件改动反复等待确认。
- 发布/生产：把本地通过、可合并、已部署和真实用户验收分开报告。

普通开发计划保持短而可执行，固定包含：

1. `链路判断`：A、B 或 C。
2. `表结构事实`：相关表和字段。
3. `备件影响`：备件如何进入、关联、计算和展示。
4. `最小改动`：准备修改的文件和原因。
5. `验证`：要运行的测试、构建或只读数据核对。

以下情况才需要在实施前单独停下确认：

- 数据库 schema 迁移或大规模历史回填；
- 删除、覆盖、合并、恢复生产数据；
- 破坏现有 API/Excel 契约的兼容性；
- 提交、推送、合并、部署、重启服务或其他外部状态变更；
- 需求存在会显著改变结果的关键歧义。

## 5. 修改边界

- 只修改完成当前任务所需的最小文件集合，不顺手重构无关模块。
- 修改前检查 `git status`；用户已有的 tracked、untracked 和 ignored 文件都视为用户工作，不覆盖、不清理。
- 优先沿现有模块边界修改：模型/迁移、ETL、service、API、前端 API、页面、测试各司其职。
- 不为了减少文件数把业务逻辑塞进路由或 React 页面。
- 后端路由主要负责参数、权限和响应；业务查询与计算放在 `backend/app/services/`。
- Excel 解析与入库逻辑放在 `backend/app/etl/` 或对应的专用导入 service。
- 前端统一请求优先放在 `frontend/src/api/`；避免继续新增页面内散落的 Axios 调用。
- schema 变更必须有 Alembic migration；不要只改 SQLAlchemy model。
- API 响应变化同步检查后端测试、前端类型、调用页面和相关契约。
- 不用本地测试结果声称生产已经生效。

## 6. 可信内部用户的安全取舍

本项目按“可信内部用户按预设流程操作”的威胁模型开发。安全不是普通需求的主要优化目标。

- 不主动扩展恶意攻击、渗透、对抗性输入或复杂风控场景。
- 不为与当前需求无关的理论风险增加 rate limit、验证码、复杂加密、额外审批或大规模安全框架。
- 不让安全讨论挤占数据模型、业务口径、导入正确性、计算正确性和使用效率。
- 只有用户明确要求，或当前实现会直接导致凭据泄露、越权读取、数据破坏、静默算错时，才将安全问题提升为开发重点。

仍然保留以下工程底线：

- 不硬编码或输出密钥、密码、token、生产连接串；
- 不绕过或删除现有登录、权限和数据范围逻辑；
- 不未经确认执行生产写入、删除、迁移、部署或重启；
- 不牺牲数据库约束、事务、幂等、审计和可恢复性来换取表面速度。

这些底线主要保护数据和正常操作，不以假设恶意攻击为前提。

## 7. 业务事实与文档读取

开始任务时按需读取，不做无关的全仓库文档巡检：

- 全局业务语言：`CONTEXT.md`
- 通用仓库入口：`AGENTS.md`（本文件）
- Claude Code 兼容入口：`CLAUDE.md`（只做适配，不另存项目状态）
- 维保当前口径：`docs/maintenance/ARCHITECTURE.md`、`docs/maintenance/REQUIREMENTS.md`
- 明确的数据/API 契约：`.ai/contracts/` 下与任务直接相关的文件
- 架构决策：`docs/adr/` 下与任务直接相关的 ADR

`AGENTS.md` 是所有开发 Agent 在本仓库的共同执行规则。`.ai/AI_WORKFLOW.md` 说明跨 Agent 的认领、进度和交接流程；历史计划只能作为参考，不能用过期计划或缺失文件阻塞当前任务。源码、运行中数据库和当前正式业务文档优先。

## 8. 分析和解释方式

不要输出隐藏思维链；输出可检查、可复现的决策依据。

分析复杂问题时使用：

```text
现象
  -> 当前数据库/源码事实
  -> 数据链路定位
  -> 根因假设
  -> 验证证据
  -> 结论与最小改动
```

每个新概念说明三件事：

1. 它解决什么问题？
2. 它依赖哪个上游概念、表或模块？
3. 它被哪个下游服务、接口或页面调用？

解释代码时提供逐变量表：

| 名称 | 类型 | 用途 | 为什么这样定义 |
| --- | --- | --- | --- |

## 9. 验证和交付

验证范围与风险成比例：

- ETL/Excel：表头识别、映射、预检、幂等重传、目标表结果。
- 数据库/service：关键查询、边界值、`null` 与 `0`、聚合口径、事务行为。
- API：请求参数、响应结构、错误语义和权限范围。
- 前端：相关测试、TypeScript、构建及关键页面状态。
- 跨链路需求：至少一个从输入字段到页面结果的 tracer-bullet 样例。

交付时只报告：

- 改了什么；
- 数据链路和备件流转发生了什么变化；
- 哪些测试/构建/查询已通过；
- 哪些只在本地验证、哪些尚未验证；
- 是否需要迁移、回填、重导、发布或人工确认。

除非用户明确要求，不自动 commit、push、创建 PR、合并或部署。

## 10. 多 Agent 协作与共享进度

项目记忆分为三层，不把聊天历史当共同事实源：

```text
AGENTS.md / CONTEXT.md / ADR / contracts
  -> 稳定规则、业务语言和长期决策
GitHub Issue / Draft PR
  -> 当前任务、认领、进度、阻塞和下一步
commit / CI / release evidence
  -> 已发生且可复核的实现与验证事实
```

执行要求：

- 一个完整 Feature 使用父 Issue 管理业务验收、契约、依赖 DAG 和最终集成；父 Issue 不直接承载代码分支，也不挂 `agent:*` 标签。
- 数据库、后端、前端、契约和集成分别建立实现子 Issue。一个 Feature 可以并行存在多个子 Issue；“唯一 Write Owner”只约束单个实现子 Issue，不限制整个 Feature 只能由一个 Agent 开发。
- 每个写代码的 Agent 必须使用独立 worktree 和独立分支，并从明确的远端 base SHA 开始。不得让两个写 Agent 共用一个 working tree。
- 子 Issue 必须写明 `Workstream`、`Consumes`、`Produces`、`Contract SHA`、`Owned paths`、`Forbidden paths`、`Depends on` 和 `Blocks`。
- 跨层开发开始前先冻结契约。下游只能依赖父 Issue 中由 Integration Owner 确认的准确 Contract PR/SHA；契约变化必须重新冻结并通知所有受影响子 Issue。
- 默认路径归属：数据库 Agent 负责 `backend/alembic/versions/` 和 `backend/app/models/`；后端 Agent 负责 `backend/app/services/`、`backend/app/api/` 和 `backend/app/schemas/`；前端 Agent 负责 `frontend/src/api/`、`frontend/src/pages/` 和 `frontend/src/components/`。共享边界文件由 Contract/Integration Owner 明确分配。
- 开始前按 `docs/agents/issue-tracker.md` 检查子 Issue 的 Claim，并留下执行器、base SHA、branch、worktree、契约与路径边界。Claim 是可回收的协作记录，不是全仓库锁。
- 同一实现子 Issue 同一时刻只有一个 Write Owner。第二个 Agent 可以只读审查；需要接力时先留下 checkpoint，再明确移交。
- 不创建全局 `.ai/CURRENT_TASK.md` 或全局 `TODO.md` 作为并发进度真相。动态进度写入对应 Issue 和 Draft PR。
- Agent 发现不属于自己的改动、文件重叠或 base 漂移时，立即停止重叠写入，保留现场并报告，不覆盖、不清理。
- Draft PR 是子 Issue 的实现态交接页，必须写明父/子 Issue、Workstream、base/head SHA、Consumes/Produces、Contract SHA、路径边界、验证证据、阻塞项和下一步。
- 三个分层 PR 各自通过不等于 Feature 完成。Integration Owner 必须按契约、数据库、后端、前端的依赖顺序核对准确 SHA，并完成一次“迁移 -> Backend -> API contract -> Frontend -> tracer-bullet”的跨层验证。
- 开发环境默认隔离；若共享集成环境，父 Issue 必须记录环境标识、DB migration/head、Backend SHA、Frontend SHA、数据快照或 seed 版本和验证时间。
- `.ai/CHANGELOG.md` 用于阶段或发布审计，不承担实时任务调度，启动时无需全量读取。
- 本地通过、GitHub CI、可合并、已部署和真实用户验收必须分开记录。

完整状态机、认领格式和交接模板见 `docs/agents/collaboration.md`。

## Agent skills

### Issue tracker

任务、PRD、认领和进度使用 GitHub Issues/PRs。见 `docs/agents/issue-tracker.md`。

### Triage labels

使用 canonical triage 标签和独立的执行状态/Agent 标签。见 `docs/agents/triage-labels.md`。

### Domain docs

本仓库是 single-context：根 `CONTEXT.md` + `docs/adr/`，并按任务读取相关模块文档与 `.ai/contracts/`。见 `docs/agents/domain.md`。
