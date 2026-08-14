# 维保备件双闭环 Execution Ledger

更新：2026-08-14 14:30 CST

## 权威计划

- Active: `.ai/MAINTENANCE_SPAREPARTS_PARALLEL_IMPLEMENTATION_PLAN_V2.md`
- Rejected: `.ai/MAINTENANCE_SPAREPARTS_PARALLEL_IMPLEMENTATION_PLAN.md`
- 原因：独立业务架构与数据库审查发现真实数据合同顺序、仓库适配、reversal、返还政策、逐行入库分配和并行文件契约存在 P0。

## 当前执行状态

- Gate: `G0 Business/Data Contract Gate`
- 判定: `不可合并`
- 原始工作树：保留用户既有未跟踪文件；本轮只新增/更新 `.ai` 计划、合同和台账，不在该工作树做业务开发。
- 开发基线：`c431656bd2615102f053199801554191b2d88791`
- 远端 main / 当前生产目标：`caf4a9737bf62495f3c81761d37d7989bb0b765e`
- Alembic current production head: `d9f1a3c7e5b2`
- vNext DB head: 尚未冻结；G0/G1/G2 审查后从 d9 新起。

## Claude Code 多 Agent 运行

- Session: `c6bc699c-8bfd-4e4a-8303-a79e737831d9`
- Runtime alias: `fable`
- 已核对 alias mapping: `deepseek-v4-flash[1M]`
- 模式：协调器 + 独立 worktree + Agent Teams。
- 当前：协调器和 Schema/Kernel Agent 已收到暂停指令。
- 代码提交：无。Schema/Kernel Agent 曾在暂停指令到达前产生 18 项未提交草稿；已全部从其隔离 worktree 清理，Kernel HEAD/status 已核对为 `c431656b`/clean。
- 协调器会话已停止并从 Claude Agent 列表删除；不会在 G0 缺口下自动恢复。
- Integration worktree 的 `uv sync` egg-info artifact 已清理，HEAD/status 已核对为 `c431656b`/clean。
- 基线后端全量测试：`2777 passed, 6 skipped, 0 failed`（1177.10s）；这只证明 c431 基线测试健康，不代表新闭环已实现。
- 历史协调器恢复条件：完整 V2 G0/G1 完成、Shared Kernel 合同复审通过后，才重新下发业务闭环任务。

### 当前 G1a-PARSER-SANDBOX 会话

- Session: `b6ce992a-d771-46ea-94f8-6c75c7c884d4`
- Runtime alias: `fable`，已在启动界面核对为 `deepseek-v4-flash[1M] with max effort`。
- Worktree / branch: `/tmp/it-maint-spares-g1a` / `codex/maint-spares-g1a`，base `c431656b`。
- 多 Agent：唯一 Implementer + Data Contract Reviewer + Security/Performance Reviewer；Root 另有独立 Codex reviewers。
- 授权范围已收紧为 `config + adapter + parser tests`；零 service/API/bridge/schema/production apply。生产 approved contracts/metadata 保持空。
- 当前仍无 Git commit；本地冻结 adapter SHA-256=`146df4dd95d7741fa072a44c453810b5d86048428fc080ce8a21c3302dad3665`，240 项 focused tests 全绿，真实 S07/S09 资源门通过，独立审查 `P0=0/P1=0`。它只具备进入独立提交和 CI 的条件，不开放 apply。
- `APPROVED_*` 和生产 candidate 配置仍为空；outward 保持 `unknown_version`、`can_apply=false`、业务写入 0。
- `.ai` 候选合同 bundle 已由 Git commit `27dc6b842c67f190284af88398fef0941f834f1d` 首次跟踪；该 SHA 冻结文档/合同证据，不包含 G1a parser 代码提交，也不等于 source owner 或 production apply 批准。

## 独立审查结论

最终回归：业务架构 Reviewer `P0=0/P1=0`；数据库/实现 Reviewer `P0=0/P1=0`。以下条目均已在 V2 计划层闭合；这不改变 G0 外部合同失败关闭状态。

### 业务架构 P0

- 真实样表/稳定键必须前移到 schema 之前。
- 必须实现真实 shipment delivery adapter，不能继续只靠 synthetic delivery source。
- 申请硬规则必须区分采购/销售事实、现场领用频次和价格候选，不能混为一个可人工放行风险。
- 审批输出不得暗示已供货或自动导出 WBDD。
- 主库/地区库 availability 与实名申请人关系必须进入合同。
- reversal 必须使用 signed delta；剩余待收不能冒充收货差异。
- return decision 由服务端产生；好件需逐行/部分入库 allocation。
- 坏件仓库实收只闭合义务，仍是 pending inspection，不恢复库存。
- 暂估成本与财务确认成本必须分开。

### 数据库 P0

- replenishment 不可变 evidence 必须进入 schema、trigger 和 submission digest。
- 历史硬盘 v1 与项目政策 v2 必须兼容且受项目 write flag 控制。
- SN quantity/cardinality、reversal、全局锁顺序、Admin provisioning 必须可执行。
- command/event/policy/allocation 需要 FK、组合一致性、index、CHECK 和 immutable trigger。
- candidate/preview/confirm 必须切换到同一 ledger read model。
- migration 使用 nullable expand、lock timeout、必要时 NOT VALID FK；新事实存在时 downgrade 失败关闭。

## G0 实证登记

- 已生成 `source-registry.yaml`、`state-mapping.yaml`、`stable-relationship-matrix.md`、`product-decisions.md`、`sample-manifest.sha256`；均只含 metadata/聚合值/SHA，不含客户、PN、SN、金额或原始文件名。
- S02/S03/S05 的 header/line stable ID 在生产 null/duplicate 均为 0；当前只认原始状态 `已生效`。S05 active WBDD 中 18,526 条有稳定项目归属、100 条失败关闭。
- S04 虽有不重复产品库存 ID，但现 loader 按 `(pn_std, warehouse display label)` 聚合 upsert，未持久化来源状态，且 upload date 不是已证明的库存 as-of；不能作为公司/地区可用量权威合同。
- 此前生产上传卷扫描没有获得可消费 S07/S08/S09；对应 warehouse batch/document/line/ambiguity 生产表仍为 0 行。此事实已被后续用户附件补充，但生产尚未登记/批准这些合同。
- S07/S09 已有用户提供的真实双表头、状态聚合、稳定键和 SHA；仍缺正式 source owner、生产 approved metadata、可安全处理真实规模/merge/controlled formula 的 parser，以及显式 correction identity。S08 已裁定为 V1 optional 外部对账。因此整体 G0 继续 `failed_closed`。
- S02/S03 仍缺全量/增量、覆盖区间、source as-of、最新完整业务日、导出频率、freshness SLA 和断批证明；在 `coverage_state=complete` 前，系统不能把“无数据”硬判成“90 日无采购销售”。
- 在用户补充附件之前，限定生产目录搜寻没有新增证据；现在以用户补充附件的 SHA/双表头审计作为最新事实，不再把 S07/S09 标为“无样表”。
- 产品裁决已按用户授权冻结：`[D-90,D)`、有效状态仅 `已生效`、硬拦截不可覆盖、正常项由不同实名审核人逐行审批、无 LLM 自动审核；这些裁决不替代缺失的数据来源合同。

### 用户补充的真实仓库样表

- 已收到并只读审计真实 S07、S08 宽/窄、S09 与后置 S06 样例；原文件未修改、未复制入 repo、未输出业务行。
- S07 candidate header signature 由 required-code selector 识别为 `shipment_v1`，19,572 个稳定单据头、69,298 个稳定明细 ID，ID/单号冲突为 0；状态聚合为已生效 19,570、草稿 2。它尚不是 production-approved contract。
- S09 candidate header signature 由 required-code selector 识别为 `receipt_v1`，10,177 个稳定单据头、82,910 个稳定明细 ID，1 行缺 line ID；状态聚合为已生效 10,107、草稿 5、已取消 65。它尚不是 production-approved contract。
- S08 宽表命中 `return_v2`，有稳定头/行 ID 但公式阻断；窄表命中 `return_v1`，因完全没有稳定头/行 ID 保持 non-authoritative。S08 仍是 V1 optional。
- 真实样表暴露三个 parser blocker：S07/S09 超过当前 500 万 cell 上限；49,726/72,734 条续行依赖纵向 merge；公式仅出现在附件、图片或测试报告类证据字段。现 parser 会整本拒绝或漏续行。
- G0 状态仍 failed-closed；仅 G1a-P parser sandbox TDD 获准：不写真实 approved 配置、不改 apply/API/service/bridge、不建 schema、不触碰生产。
- 2026-08-14 最终冻结 parser benchmark：S09 82,910 条有效行，157.602 秒、RSS delta 187,768 KiB；S07 69,298 条有效行，146.673 秒、RSS delta 179,004 KiB。两者均低于 wall 180 秒、hard timeout 240 秒和 RSS delta 768 MiB 门；这只关闭 parser sandbox 的资源风险，不批准来源合同或生产 apply。

## 生产只读审计（未写入）

- 生产 v1.21 frontend/app/db 运行中且 restart count 为 0；runtime image IDs 与 release state 一致。
- deployed target SHA=`caf4a9737bf62495f3c81761d37d7989bb0b765e`；DB head=`d9f1a3c7e5b2`。
- PostgreSQL 15.18 accepting connections；无 >60s transaction、blocking/ungranted lock；无 processing import job。
- uploaded-files volume 约 359 MB / 292 files；其中 Excel 193 个。`sys_raw_file` 有 151 个 Excel metadata rows。物理文件与 metadata 不一一对应，禁止按“最新文件”猜权威样例。
- 最近成功导入只是一条强候选；必须按 source type、raw file ID、import batch 和 source registry 确认后才能用于 G0。
- 现有日备份有可验证 DB custom dump + checksum，但不包含 uploads；因此不是用户要求的全量备份。
- 现有 restore drill 只验证 DB，不验证 uploads，且是在 live PostgreSQL 内建临时库；vNext 必须改成隔离 PG + 临时 uploads 恢复。
- 生产 checkout 是脏的 detached 运维目录；不得在生产 checkout build/pull/reset/clean。发布真相只取 signed release state、runtime image IDs 和 DB head。

## 下一动作

1. 独立复审 `.ai` candidate bundle commit `27dc6b842c67f190284af88398fef0941f834f1d`；这不开放 apply。
2. 将冻结 G1a-P 代码作为独立候选提交，复跑 focused/full CI 和双 Reviewer；生产配置继续为空。
3. 补齐 S02/S03 coverage、S04 稳定仓库、S05→S07 关系、S07/S09 source owner/revision/检测枚举，复审完整 G0。
4. 完整 G0 后执行 G1a-R relation/delivery；通过后才串行 G2/G1b，再从 reviewed `KERNEL_SHA` 同时启动 Lane A/B/C。

## 生产停止条件

当前没有 release candidate Git SHA、DB_TO、完整 uploads 备份、隔离恢复、真实账号 canary 或观察证据；因此任何生产写入/部署均禁止。
