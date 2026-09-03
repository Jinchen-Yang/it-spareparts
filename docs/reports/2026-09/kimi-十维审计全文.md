# glm-backend 10 维审计结果

> **校正（2026-09-03，Claude 复核）**：本文由 kimi code 子 agent 生成，Claude 对 high 级发现做了抽检。维度 3 的「alembic 8 头并存、main 上 `upgrade head` 不可执行」**不成立**：用 alembic 自身 `ScriptDirectory.get_heads()` 核实，当前 96 个迁移**单头** `f6b1d3e8a2c4`。kimi 漏解析了多行 tuple 形式的 `down_revision`。仓库确有 6 个 merge 迁移，说明历史上反复出现过多头，但现在不是。该维度其余发现（a8e4 仅在未合并分支、状态词表 active/ongoing 双轨、金额列无税口径后缀）已抽检成立。维度 5「缺行=作废」与维度 6「32 个死文件」抽检成立。综合决策见 `outputs/2026-09-03-重构决策审计报告.md`。

审计日期：2026-09-02/03。审计基准：工作区 HEAD（`80d65dc`，fix/maintenance-future-start-lifecycle）与 `origin/main`（tip `c9d67fe`）；全程只读，未修改任何文件、未执行 git 写操作。方法：10 个维度各由一个子 agent 独立读码/跑只读命令完成，本文件为主 agent 归拢稿。

## 0. 总览

**整体判断：这是一个「单兵高强度救火循环」的仓库，但火是局部的，不是系统性腐烂。** 8 月一个月产生了全史 57% 的提交（282/497），fix:feat 从 7 月的 0.65 恶化到 8 月的 2.08，main 分支曾因「侧分支直接部署生产」冻结 6.4 天酿成 09-02 事故；但热点高度集中在维保模块的少数上帝文件（operations 7199 行、master_workbook 5269 行、roundtrip 4784 行、permissions 1087 行），其余代码演进平缓。架构基底本身是健康的：services 层零循环导入、117 张表带 350 个 CheckConstraint 与 107 处唯一约束、3000+ 以真实库为主的行为型测试、双 lock + hash 校验的依赖链。**10 个维度全部给出 favors_incremental 信号——没有任何一个维度需要推倒重写。** 正确的路径是：先补流程护栏（发布校验、main 分支纪律、决策日志），再按限界上下文渐进拆分 3-4 个上帝文件，同时定点补测试网。

| # | 维度 | rewrite_signal | 一句话结论 |
|---|---|---|---|
| 1 | 仓库结构与卫生 | favors_incremental | 主干清晰，卫星结构失修（5 套文档体系、.deploy 四代脚本并存、分支堆积） |
| 2 | 后端架构与代码质量 | favors_incremental | 分层基底干净（零环零反向依赖），病灶是 3 个上帝文件 + api 层 339 处直写 DB |
| 3 | 数据库模型与迁移 | favors_incremental | 约束资产丰富，但 8 个 alembic 头并存、事故迁移 a8e4 不在 main |
| 4 | 权限系统 | favors_incremental | 守卫层质量高，45 键逐账号勾选的表达层需中型收敛（3-5 人日） |
| 5 | 维保领域口径 | favors_incremental | 代码执行「删行=作废」（与 CONTEXT.md 矛盾）；整本 CAS 造成 409 死循环 |
| 6 | 前端架构 | favors_incremental | 骨架健康，12.6% 是死代码（6600 行），测试是行为式安全网 |
| 7 | 依赖与构建/部署链 | favors_incremental | 锁链成熟，但 v123 发布脚本零 git 校验零测试闸门 |
| 8 | 测试与重构安全网 | favors_incremental | 3229 个行为型测试可作安全网，但 bulk_import/identity 近乎裸奔、本机不可跑 |
| 9 | agent 协作治理 | favors_incremental | 无跨工具共享记忆，事故模式在审计当天仍在重演（决策只存在 zcode 未入库 plan） |
| 10 | git 历史与变更模式 | favors_incremental | 救火循环属实但局部化；7 月数据证明同一套代码可健康演进 |

跨维度交叉验证的三点一致性：① 维度 3/5/10 各自独立确认了「双头基线」事故链（main 冻结 6.4 天、侧分支 29 提交、a8e4 迁移游离于 main 之外）；② 维度 5 与维度 9 独立得出同一结论——代码执行的是「删行=作废」，`CONTEXT.md:134` 的「缺行不代表删除」是过期口径；③ 维度 2/8/10 独立确认热点文件与巨头文件完全重叠，拆分优先级一致（operations → master_workbook → 观察 roundtrip）。

---

## 1. 仓库结构与卫生

仓库主干（backend/ + frontend/ + docs/ + .deploy/）分层清晰，但周边"卫星结构"明显失修：文档/契约体系同时存在 5 套以上（根级三份架构报告、CONTEXT.md+docs/adr、.ai/contracts、docs/ 各专题目录、.zcode/.claude 计划），其中 `.ai/AI_WORKFLOW.md` 要求 Agent 必读的 7 个文件全部不存在，与 CLAUDE.md 声明的现行约定直接矛盾。`.deploy/` 呈"每发一版复制一整套脚本"模式，v120–v123 四代共 24 个脚本约 6965 行 shell，无共享库。分支共 44 个引用，4 个已合并入 origin/main 未删；本 worktree 本地 main 落后 origin/main 42 个提交。`outputs/`（17 个文件）未跟踪也未 gitignore；主仓另有 7 项未跟踪。docs/ 下 15 个子目录中 8 个最后提交停留在 7 月，属一次性产出，全库仅 `docs/maintenance/archive/` 一处有归档机制。

### 指标

- 根级辅助目录文件数：.ai 41 / .claude 9 / .zcode 2 / .deploy 39 / docs 124 / outputs 17（未跟踪）/ scripts 2
- 文档体系套数：≥5
- `.ai/AI_WORKFLOW.md` 引用的不存在文件：7/7 全部 MISSING
- docs/adr 重号：2 个 `0002-*.md`
- .deploy v12x 脚本：24 个文件，6965 行（v122 release 单文件 2893 行）
- git 分支：本地 21 + 远程跟踪 23 = 44；已合并 origin/main 未删 4 个；抽样 6 个分支中 2 个经 squash 合入仍残留
- 本地 main 落后 origin/main：42 个提交
- 主仓未跟踪条目：7 项（含 AGENTS.md、output/、outputs/、tmp/）
- docs/ 子目录最后提交 ≤7 月：8/15

### 发现

- **[high] .ai/AI_WORKFLOW.md 指向一套不存在的上下文协议（dead protocol）**
  - 证据：`.ai/AI_WORKFLOW.md:11-21` 要求必读的 7 个文件逐一 `test -e` 全部 MISSING；`CLAUDE.md:27` 声明现行约定是"根 CONTEXT.md + docs/adr/"
  - 影响：新接入的 Agent 按协议执行第一步即失败，两套"权威入口"互相打架
  - 渐进修法：删除或重写 AI_WORKFLOW.md 指向 CLAUDE.md/CONTEXT.md；`.ai/contracts` 迁到 `docs/contracts/`
- **[high] .deploy/ "发版即复制一整套脚本"，四代并存无共享库**
  - 证据：v120 系列 14 个、v121_beta 5 个、v122 5 个、v123 4 个，合计 6965 行；v122→v123 normalized diff 达 3053 行——每版都是 fork 而非参数化
  - 影响：发布逻辑演进无法 diff 审阅；修公共 bug 要改 N 份拷贝
  - 渐进修法：抽出 `release_lib.sh` + 每版一个 `releases/<version>.env`；旧脚本移入 `.deploy/archive/`
- **[medium] ADR 编号冲突**：`docs/adr/0002-manual-source-order-project-assignment.md` 与 `0002-stable-project-cost-evidence.md` 同号并存 → 重编号为 0004 并加 supersedes 注记
- **[medium] 分支堆积 + 本地 main 落后 42 提交** → 删已合并分支；worktree 内对齐 main；CI 加"合入后删分支"惯例
- **[medium] 根级三份架构报告陈旧（5–6 月）且无引用**，与 docs/maintenance/ARCHITECTURE.md 双头并存 → 移入 `docs/reports/archive/2026-H1/`，指定唯一现行架构文档
- **[medium] outputs/ 未跟踪、未 gitignore，主仓还有 output/+outputs/+tmp/ 三处**（目录名都不统一）→ 统一 `outputs/` 并 gitignore，或需追溯产物入 `docs/reports/`
- **[medium] docs/ 无统一归档机制，8/15 子目录为一次性产出** → docs/ 根加 README 索引标注状态；停滞 >60 天专题移入 `docs/archive/`
- **[medium] 主仓 AGENTS.md 未跟踪，本 worktree 根无 AGENTS.md** → 选定单一 AGENTS.md 提交入库（详见维度 9）
- **[low] 计划/评审类一次性产物散落 5 个位置**（.ai/review、.ai/claude-prompts、.zcode/plans、docs/superpowers/plans、outputs）→ 约定 `docs/plans/YYYY-MM/` 单点存放
- **[low] worktree 脏文件含审计工作流自身产物，易误提交** → 审计产物放仓库外或 `.git/info/exclude`

### 信号：favors_incremental

理由：问题性质是"积累未清扫"而非"结构错误"。核心业务目录边界清楚，docs/maintenance 内部已有 archive/contracts/bugs/reports 的良好自治实践。全部 10 项发现都可用删除、移动、gitignore、重编号这类可逆小步修复；失修集中在 agent 工具配置与过程产物——重写仓库解决不了"卫生惯性"。

---

## 2. 后端架构与代码质量

backend/app 共 117,463 行，其中 services/ 84,391 行约 105 个模块、api/ 16,294 行 51 个路由文件、models/ 仅 6,909 行——业务逻辑几乎全压在 services 层。分层大方向健康：**services 全目录零循环导入**（Tarjan SCC 检测无 >1 的环），且 services 对 api **零反向依赖**。核心问题：`maintenance_project_operations.py`（7,199 行/97 函数）是典型上帝对象；api 层直接操作 DB 证据充分（33 个文件 339 处 `db.*` 写调用、24 个文件 71 处 `select(`）；维保"工作簿"存在 4 套并行协议且适配器模式不统一。另有 628 处 legacy/fallback/兜底分支散布 84 个文件，主要集中在 migration 系列。测试代码 132,531 行，覆盖基础扎实。

### 指标

- backend/app 总行数 117,463（测试 132,531）；services/ 84,391 行 / ~105 模块
- services 行数 top3：maintenance_project_operations.py 7,199；maintenance_project_master_workbook.py 5,269；maintenance_roundtrip.py 4,784
- api 层直接 DB 写调用：339 处 / 33 文件；内联 `select(`：71 处 / 24 文件
- services 循环导入：0 个环；零引用死模块：0 个
- 工作簿协议数：4 套（`PROJECT_WORKBOOK/2.0`、`PROJECT_MASTER/2.0`、`MANAGER_WORKBOOK/3.0`、roundtrip 协议）
- legacy 273 / fallback 131 / compat 24 / 兜底 70 = 628 处 / 84 文件
- TODO/FIXME/HACK 真实仅 1 处（dashboard.py:667）
- 2025 年来 churn：operations 51 次提交、master_workbook 32 次、roundtrip 5 次

### 发现

- **[high] `maintenance_project_operations.py` 是上帝对象**
  - 证据：7,199 行、97 函数，可归出 8 项职责（现场领料/费用/合同 CRUD/回款/成本缺口/工作簿版本锁/读模型聚合/历史回填）；2025 年来 51 次提交全仓最热
  - 影响：合并冲突与回归风险集中；幂等、审计、锁、读模型与事实写入耦合
  - 渐进修法：按职责簇抽 6-8 个模块，原文件做 re-export 门面，逐个迁移调用方
- **[high] api 层大面积直写数据库，分层失守**
  - 证据：api/ 下 339 处 `db.commit/execute/query` 覆盖 33/51 路由文件；典型 `api/maintenance_project_operations.py` 单文件 74 处、`api/imports.py` 36 处（:399-416 同一请求多次 commit）
  - 影响：事务边界散落 HTTP 层，业务规则易被绕过，无法脱离 FastAPI 测试
  - 渐进修法：以这两个文件为先下沉 SQL 到 service；立"api 禁止 import sqlalchemy select"lint 规矩
- **[high] `maintenance_project_master_workbook.py` 双协议共存**：5,269 行内含 v1 与 v2（:75 `V2_PROTOCOL_ID`），共享私有 helper 易互伤 → 机械拆分 v1/v2/common 三文件，冻结 v1
- **[medium] `maintenance_roundtrip.py` 安全关键代码与业务混杂**：XXE/炸弹防护（:1781）、HMAC 签名埋在 4,784 行业务文件中 → 抽 `roundtrip_security.py` / `roundtrip_signing.py`
- **[medium] 工作簿 4 套并行协议、适配器模式不统一**：4 个协议 ID（workbook_v2.py:30、master_workbook.py:75、manager_workbook_v3.py:32、roundtrip.py:59）；仅两套有事务适配器 → 以 v2 adapter 的事务计划模式为基准对齐，v1/roundtrip 的退役决策写进 ADR
- **[medium] legacy/fallback/兜底分支 628 处且集中在 migration 系列**（migration_legacy 63 处、maintenance_cost 50、migration_source 41）→ migration 系列移入 `services/legacy/` 子包冻结；manual_fallback 设下线指标
- **[medium] `maintenance_` 扁平命名空间混杂 6+ 限界上下文**：105 个模块中 64 个 `maintenance_` 前缀平铺 → 建 `app/services/maintenance/` 子包，import 重写 + 兼容 re-export 分两批落地
- **[low]（正向）无循环导入、无反向依赖、无死模块、TODO 近零** → 将 import 环检测固化进 CI
- **[low] 历史热点与体量热点重合**：拆分顺序按 operations → master_workbook →（观察）roundtrip

重构切分方案（限界上下文）：备件主数据 / 标准化·ETL / 库存·采购·补货 / 维保项目核心 / 维保工作簿·导入（主战场）/ 维保成本财务 / 迁移·数据质量（冻结）/ 平台。

### 信号：favors_incremental

理由：架构方向正确（零环、零反向依赖），有 132,531 行测试做安全网；问题集中在 3 个上帝对象、api 层事务下沉不足、工作簿多协议并存——都是 strangler 式渐进拆分可解的。重写会丢失 roundtrip/master_workbook 中大量隐性业务规则（签名、幂等、XML 安全）。唯一需"退役式"处理的是 migration legacy 系列，建议冻结而非重写。

---

## 3. 数据库模型与迁移

Schema 本体健康度出人意料地高：117 张表、350 个 CheckConstraint、107 处唯一约束、181 个外键，维保身份链核心环节（XSDD→项目）已由数据库触发器 + 主键兜底。真正的重灾区在**迁移治理**：main 分支当前 **8 个 alembic head 并存**；事故报告中的 `a8e4f1c7d3b9` 在 main/HEAD 中**根本不存在**，只存在于未合并分支 `codex/master-workbook-full-edit`（commit `9b30126`），其 `.pyc` 还残留在 `__pycache__`；`f6b1d3e8a2c4` 在代码中真实存在且是当前 8 头之一。命名与词表是中等债务：约 20 个金额列无税口径后缀、`contract_amount` 实为未税、状态值无任何 Enum 集中定义且 lifecycle 存在 `active` vs `ongoing` 两套并存词表。

### 指标

- 模型表总数 117（维保 77 / 备件·进销存 29 / 系统·权限 9 / 聊天 2）
- 迁移文件数 96；**alembic head 数：8**（其中 3 个本身是 merge 迁移后又长出分支）
- 含 `op.execute`/`bulk_insert` 的迁移 63；含 DML 回填 32；创建触发器/DDL 函数的迁移 15
- CheckConstraint / Index / Unique / ForeignKey = 350 / 189 / 107 / 181
- backend/app 内 Enum 类定义：0；同一列被 ≥3 个迁移反复增删改：9 列（最高 `f_maintenance_line.cost_bucket` 10 次）
- 模型层零引用表：实质 0（2 张一次性修复工具表被 scripts/tests 引用）

### 发现

- **[high] 迁移链 8 头并存，`alembic upgrade head` 在 main 上不可执行**
  - 证据：heads = `e6f1a9c3b7d2`、`f5a7c9e1b3d4`、`c8f2d4a6b9e1`、`b6e2d9f4a1c7`、`f6b1d3e8a2c4`、`a6d1e9c3b7f2`、`e4f6a8c2d1b3`、`d3e5f7a9c1b2`（解析 96 文件 revision 链，无孤儿引用）
  - 影响：任何按 head 升级的部署直接失败或只走单分支；生产库 alembic_version 指向不可预知——正是 09-02 事故的土壤
  - 渐进修法：写多 parent merge 迁移收敛单头；CI 加 `alembic heads | wc -l == 1` 卡口
- **[high] 事故报告核实：`a8e4f1c7d3b9` 不在 main，`f6b1d3e8a2c4` 是合法 head**
  - 证据：a8e4 仅在 `codex/master-workbook-full-edit` 分支（`9b30126`），残留 `__pycache__/a8e4f1c7d3b9_backfill_project_periods.cpython-313.pyc`；f6b1 存在于 `versions/f6b1d3e8a2c4_salesperson_override.py:13` 且是 8 头之一
  - 影响：a8e4 的期限回填（45 项目）已物理执行但版本表未推进——合入 main 会重跑回填，不合入则修复永远游离
  - 渐进修法：在 main 重写幂等回填迁移（`UPDATE … WHERE period_from IS NULL`）或对已回填库 `alembic stamp`；删 `__pycache__` 残留
- **[medium] 身份链逐环节判定：核心有约束，非标准合同号退化回代码约定**
  - 证据：XSDD→项目有约束（`models/maintenance_project.py:118` 主键 + 触发器 `d2c7e9f1a4b6:232-245`）；WBDD 挂靠有部分唯一索引（`maintenance_source_assignment.py:63-68`）；AUTO 项目有 advisory lock + claim（`maintenance_source_assignments.py:1381-1383,1562`）；回款/领用均有唯一约束（`maintenance_project_operations.py:80-83,253,257-263`）；但 `contract_no` 无唯一索引，且触发器对非标准格式合同号返回 `''` 直接放行（`d2c7e9f1a4b6:204-205`）
  - 影响：标准 XSDD 单号数据库级安全；非标准合同号可挂多项目
  - 渐进修法：对 normalize 结果非空建表达式部分唯一索引；非标准号纳入统一 normalize 规则
- **[medium] 金额命名：约 20 列无税口径后缀，`contract_amount` 名实不符**
  - 证据：`contract_amount` 实为未税（`models/maintenance_project.py:209-211` 自述）；歧义列含 `amount`(3)、`cost_amount`(4)、`collected_amount`、`receivable_amount`、`unit_cost`(4) 等；精度已统一 `Money=Numeric(14,2)`（`_types.py:4`）
  - 渐进修法：新列评审强制后缀；存量加 comment，高频歧义列走 rename 迁移
- **[medium] 状态词表零集中，lifecycle 存在 `active` 与 `ongoing` 两套并存词表**
  - 证据：Enum 类 0 个；102 处 CheckConstraint 各写字面量；存储列写 `active`（`maintenance_collection_plan_imports.py:794-798`），计算/过滤侧认 `ongoing`（`maintenance_periods.py:57-60`、`maintenance_cost.py:855`）——存储值与过滤值永不匹配
  - 渐进修法：建 `app/constants.py` 集中词表；统一 lifecycle 并回填存量
- **[medium] 同一业务概念反复迁移落地**：cost_bucket 被 3 个迁移操作 10 次；期限列 4 次落地（含分支私货 a8e4）；32 个迁移含 DML 回填，含按单据号硬编码修数迁移（`b3f8e1d6c4a2`、`c4d9a2e7f1b0`）→ 约定"结构走迁移、数据走幂等脚本"；收敛单头后 squash 基线
- **[low] 零引用废表实质为 0**，仅 2 张修复留痕表游离 → 修复战役结束后归档
- **[low] 项目 FK 靠手工清单防呆**：`_SUPPORTED_PROJECT_FK_TABLES` 手工枚举 38 张表（`maintenance_project_identity.py:63-103`），靠启动校验 + pg_constraint 对账 → 保留，启动校验提为 pytest 用例

### 信号：favors_incremental

理由：约束资产（350 CK / 107 unique / 181 FK / 15 个触发器迁移）已把核心业务不变量编码进数据库层，重新建模会丢失这些经事故检验的护栏。真正的债务三处都可渐进修复：8 头收敛（一周量级）、命名/词表止血、"结构走迁移、数据走幂等脚本"约定。没有任何一处需要推倒表结构。

---

## 4. 权限系统

权限系统是「45 个布尔键 × 逐账号勾选」的细粒度模型：5 data + 15 page + 23 action + 2 行级键（`backend/app/permissions.py:14-110`），叠加 6 个硬编码角色（`models/system.py:33`）、DB 职位模板快照和每账号稀疏覆盖三层来源。后端守卫体系本身相当严谨（277 个路由几乎全覆盖，实名白名单、依赖组合校验、字段级脱敏俱全），但代价是复杂度爆炸：同一操作最多叠 5 层门禁，且存在 3 个僵尸键（其中 `page_parts` 是「前端藏菜单、后端不拦」的假权限）、9 处 `require_admin` 硬编码绕过键体系。老板「看不懂」的根因不是缺文档（45 键都有八要素业务语言），而是三套并行概念 + 一堆隐式例外。

### 指标

- 权限键总数 **45**（data 5 / page 15 / action 23 / row 2），全清单：
  - data_*：`data_supplier`、`data_customer`、`data_purchase_cost`、`data_profit`、`data_pool_price_governance`
  - page_*：`page_parts`、`page_purchases`、`page_profit`、`page_inventory`、`page_chat`、`page_import`、`page_governance`、`page_master_data`、`page_maintenance`、`page_boss_board`、`page_pool_analysis`、`page_maintenance_boss`、`page_maintenance_beta`、`page_replenishment_beta`、`page_accounts`
  - action_*：`action_pool_manage`、`action_pool_set_policy`、`action_account_manage`、`action_data_quality_review`、`action_maintenance_roundtrip_apply`、`action_maintenance_manager_workbook_apply`、`action_maintenance_project_manage`、`action_maintenance_demand_delete`、`action_maintenance_site_issue_manage`、`action_maintenance_bad_return_manage`、`action_maintenance_acceptance_submit`、`action_maintenance_acceptance_review`、`action_maintenance_acceptance_checklist_import`、`action_maintenance_warehouse_manage`、`action_maintenance_migration_review`、`action_maintenance_ledger_import`、`action_maintenance_doc_import`、`action_maintenance_wbdd_import`、`action_maintenance_expense_collection_upload`、`action_maintenance_collection_follow_up`、`action_maintenance_collection_plan_import`、`action_replenishment_create`、`action_replenishment_review`
  - row：`own_customers_only`、`own_maintenance_projects_only`
- API 路由 277 个（53 个文件）；僵尸键 3；叠 2+ 键端点 32 个；`require_admin` 硬编码 9 处 + `require_roles` 1 处；最深门禁 5 层
- 权限相关测试文件 106/253；前端权限中心展示 43/45 键

### 发现

- **[high] `page_parts` 是服务端假权限：勾选无效，后端零消费**
  - 证据：除 `permissions.py` 定义外后端 0 次引用；`/parts` 读端点（`api/parts.py:27/74/92/110/122`）只挂 `current_role`；仅控制前端菜单显隐（`frontend/src/nav.tsx:149`）
  - 影响：权限中心「所见非所得」，直接调 API 仍可查全部型号——摧毁非技术负责人对勾选框的信任
  - 渐进修法：5 个读端点补 `require_page("page_parts")`（模板默认 True，行为零变化）
- **[high] 角色与逐键双轨并行，角色被硬编码引用**
  - 证据：6 个内置角色 + `ROLE_TEMPLATES`（`permissions.py:208-356`）+ `SysRoleTemplate`；但 `require_admin` 9 处、`require_roles("purchaser")` 1 处（`api/substitutes.py:11`）、`FULL_SCOPE_ROLES` 硬编码行级全范围（`security.py:42`）
  - 影响：约 10 个看不见的暗开关，权限中心无法覆盖
  - 渐进修法：逐个映射到 action 键；FULL_SCOPE 由行键推导而非角色白名单
- **[high] 三层来源叠加 + admin 特判，有效权限不可心算**
  - 证据：有效权限 = 模板快照 ⊕ 稀疏覆盖（`permissions.py:1042-1048`）+ legacy JSONB 回退；admin 恒全开但 4 个键例外（`security.py:124-132`）；sales 代码兜底模板与 DB 迁移 a9e2f7c4d1b8 有意不一致（`permissions.py:271-309`）
  - 影响：同一角色同一勾选因 token 新旧/快照有无/是否 admin 产生 4 种结果
  - 渐进修法：权限中心加「有效权限解释器」视图（最终值 + 来源标签）
- **[medium] 两个死 action 键占注册表**：`action_maintenance_acceptance_review`（自述已废弃，`permissions.py:918-926`）与 `action_maintenance_acceptance_checklist_import`（`api/maintenance_acceptance_checklist.py:30-33`）→ meta 接口标记 deprecated 并从批量授权候选剔除
- **[medium] 最深 5 层门禁、依赖拆在 4 张映射表**：`ACTION_DATA_DEPENDENCIES`（:392，且 402/405 行重复键静默覆盖——这是个 bug）、`ACTION_PAGE_DEPENDENCIES`（:415）、`ACTION_ADDITIONAL_PAGE_DEPENDENCIES`（:440）、`PAGE_PAGE_DEPENDENCIES`（:458）→ 合并为单张「键→依赖集」表
- **[medium] 数据范围可表达「负责人+销售对本人项目全量可改」，但身份锚点脆弱**
  - 证据：行级范围已实现（`maintenance_project_assignments.py:128-144,176-198` + `enforce_maintenance_project_access`，`api/maintenance_project_scope.py:11-27`）；但 salesperson 匹配靠 token `name` 字符串相等（`security.py:30,88-90`），改名即漏；admin/boss 恒全范围不可收紧
  - 渐进修法：销售本人改账号级 assignment；行键对 FULL_SCOPE 角色也生效（默认关）
- **[low] 32 个端点叠 page+action 双键**：18 个维保动作全依赖同一 `page_maintenance`，page 键近乎冗余 → UI 折叠分组；中期评估"page 管读、action 自带准入"
- **[medium] 45 键 × 5 组信息量超出非技术用户负荷**：八要素元数据质量高（45/45 覆盖），但操作组单组 15 键 → 首页加「岗位场景预设卡」+「能力问答式」只读视图，矩阵降为高级模式

权限模型提案（角色×模块×动作矩阵）及迁移成本：守卫函数零改动（键继续作为底层真值，矩阵只是键的组合视图）；`permissions.py` 模板重组 ~400 行、`accounts.py` meta 矩阵化 ~100 行、前端 `PermissionMatrix.tsx` 新增矩阵/场景卡视图 ~300 行、1-2 个 alembic 迁移把 5 个内置模板映射为矩阵预置行；存量快照天然兼容。量级：8-12 个文件、3-5 人日。前置：先修掉假权限/死键/暗开关（发现 1-3），否则矩阵化会把不一致固化进去。

### 信号：favors_incremental（守卫层）+ 模型表面收敛可做一次中型重写

理由：守卫机制（require_page/require_action/实名白名单/字段脱敏/行级范围）实现质量高、失败关闭方向正确，有 106 个测试文件锁定行为契约，推倒重来的回归成本巨大。需要"重写"的只是表达层：45 键逐账号勾选 → 「角色 × 模块 × 读/写/敏感数据」矩阵视图。

---

## 5. 维保领域口径（最关键维度）

维保域的「身份—期限—工作簿」三条主线中，身份线（XSDD 即项目身份）经 `33495d1`（09-02 04:14）刚刚统一，代码实现与 CONTEXT.md 口径一致但落地仅一天；期限线经 `80d65dc` 修正了「未来项目误挂期限缺失」，但 missing 残余路径仍在（手工建项硬编码 missing 且无期限录入入口）；工作簿线是最大冲突点——**代码实际执行的是事故报告描述的「删行=作废」语义**（02–06 五张表全部缺行隐式 VOID），与 `CONTEXT.md:134`「缺行不代表删除」直接矛盾，且整本 CAS + 导入全面 bump revision 的机制（`3036183` 起）造成生产一夜 409×18 的冲突死循环，失败尝试零审计。客户需求「销售订单导入建项目」在 main 上不存在，仅在未合并分支 `codex/sales-xsdd-auto-project` 中。

### 指标

- 建项代码路径（main）：4 条（手工 API、台账导入、WBDD 自动挂靠、挂靠补投影）；在途 1 条（sales XSDD 自动建项，未合并）
- `bump_locked_workbook_revision` 生产调用点：22 处（21 服务层 + 1 API 层）
- 缺行=作废覆盖：02/03/04/05/06 五张业务 sheet 全部
- 工作簿可写字段面：01 仅「合同总额」单格（限唯一/当前/未共享合同）；项目主档字段工作簿不可改
- validate/apply 权限键：同一 `action_maintenance_expense_collection_upload` + `data_profit`（另有项目范围 scope 检查）
- 失败审计：stale_workbook 等拒绝路径 0 条审计（实证：18 次 409 无痕）

### 应然口径 vs 代码现状对照表

| 口径项 | 应然（出处） | 代码现状 | 一致？ |
|---|---|---|---|
| 项目身份=XSDD 唯一归并键 | CONTEXT.md:45-47 | 33495d1 起 WBDD 按 XSDD 分组（`maintenance_source_assignments.py:1149-1166`），claim fail-closed（`maintenance_project_identity.py:260-307`），2157239 加 DB 触发器 | 一致（但 09-02 才落地） |
| 同名不等于同项目 | CONTEXT.md:49-51 | `record_alias`/别名表（`maintenance_project_identity.py:310-336`） | 一致 |
| 销售订单导入→建维保项目 | 事故报告客户需求 | main `pipeline.py:365` 仅 MAINTENANCE 触发；实现在分支（`maintenance_bulk_import.py:2400`）未合并 | **不一致（在途）** |
| WBDD 可先于 XSDD 建项，后续归并 | 设计路径 | WBDD 导入无 owner 建 AUTO-xxxxx 并 claim XSDD（`maintenance_source_assignments.py:1382-1392,1545-1590`） | 一致 |
| 未来起始项目不算期限缺失 | 80d65dc 意图 | `maintenance_periods.py:45-80`：missing 只指起止皆空 | 一致 |
| 手工建项应能录入期限 | 隐含 | `maintenance_project_catalog.py:125-153`：create 无期限入参，`lifecycle_status="missing"` 硬编码 | **不一致（缺口）** |
| **缺行不代表删除** | **CONTEXT.md:133-135** | **缺行=作废**：02（:4202-4241）、03（:4249-4262）、04（:4264-4274）、05（:4128-4201）、06（:4111-4120）全部隐式 VOID；使用说明明文「删行=作废」（:2364-2377） | **不一致（代码执行事故报告口径）** |
| 整本先预检、单事务写入 | CONTEXT.md:134 | validate/apply 两阶段 + CAS 零写入（`master_workbook.py:4538-4550`） | 一致 |
| 行级冲突校验 + 强制接管 | 事故报告 §3.2-3 | 整本 CAS，revision 不符即 409 零写入；无强制接管入口 | **不一致** |
| 除系统/身份/派生字段外全部可改 | 事故报告 §3.2-1 | 01 仅合同总额且限「唯一/当前/未共享」（:3990-4026）；主档字段不可改 | **不一致** |
| 全量修改权归项目负责人+销售 | 事故报告 §3.2-2 | `action_maintenance_expense_collection_upload`+`data_profit`（管理员/boss 级）；项目范围检查已具备（`maintenance_project_scope.py:30-39`） | **不一致** |
| 失败尝试全部留痕 | 事故报告 §3.2-5 | 409 路径 rollback+_fail 无审计（api:731-733）；成功才记（:734） | **不一致** |
| 导入与工作簿并行不互伤 | 事故报告 §4-8 | WBDD 导入（`loader.py:164,1245`）、需求单作废（`maintenance_demands.py:773`）等均 bump revision，手持工作簿立即过期 | **不一致** |

三个提交核查：`33495d1`（09-02，Closes #299）WBDD 按 XSDD 原子挂靠，identity +1534 行；`2157239`（08-31）DB 触发器守订单改号自动 claim XSDD；`80d65dc`（09-02）lifecycle 兜底从 missing 改 ongoing，修「5 个未来项目回填期限后仍挂缺失」。分支 `codex/sales-xsdd-auto-project` 存在（HEAD ee2c491），`git diff main...` = 233 文件/+58806/−4027——体量远超「销售 XSDD 建项」一件事，属重型在途分支。

### 发现

- **[high] F1 口径顶层文件与代码实质冲突：缺行=作废**
  - 证据：`CONTEXT.md:134`「缺行不代表删除」vs `maintenance_project_master_workbook.py:4249-4250`（「03 缺行=作废（用户 2026-08-20 拍板）」）、:4264-4274（04）、:4202-4241（02）、:4128-4201（05）、:4111-4120（06）、:2364（使用说明「删行=作废」）
  - 影响：CONTEXT.md 误导所有后续开发与审计；若有人按 CONTEXT.md「修 bug」摘掉隐式 VOID，会直接破坏客户已学会的工作流
  - 渐进修法：**改文档不改代码**——CONTEXT.md 修订为「缺行=作废（仅命中导出时签名行集）」并注明 2026-08-20/23 拍板出处
- **[high] F2 整本 CAS + 导入全面 bump = 冲突死循环，无强制接管**
  - 证据：CAS 零写入 `master_workbook.py:4538-4550`；导入 bump `etl/loader.py:164,1245`；22 个 bump 调用点；生产实证 09-02 03:16-03:21 409×18
  - 渐进修法：行级冲突校验（只校验用户改过的实体 ID 集，导出签名行集已具备该数据结构）+ 强制接管动作（新权限键 + 覆盖回执 + 审计）；勿回滚 3036183 的导入 bump
- **[medium] F3 失败尝试零审计**：409 路径无日志落库（api:731-733）→ `_fail` 前补拒绝审计，validate/apply/download 统一接入
- **[medium] F4 全量修改权限键未归位**：validate/apply 均 `action_maintenance_expense_collection_upload`+`data_profit`（api:663,710），项目负责人/销售默认无权 → 新增/下放动作键并按项目范围授权，属配置工作
- **[medium] F5 建项 project_code 规则四源并存**：手工（用户给码）、台账（项目名截断，`maintenance_ledger.py:618-621`）、自动（`AUTO-%05d`）、分支（`XSDD-{norm}`）；历史按名称建项的裂项靠归并工具（`maintenance_project_identity.py:2009`）→ 生成收口到单函数 + 存量归并完成度核验
- **[medium] F6 期限缺失残余路径（80d65dc 未覆盖）**：① 手工建项无期限入参且硬编码 missing（`maintenance_project_catalog.py:144-153`）；② WBDD 名称无日期段→(None,None)；③ 台账解析不出→missing（`maintenance_ledger.py:861-868`）；④ 分支 sales 建项期限可空 → 手工建项表单加期限可选入参（update 已支持，:190-191）
- **[medium] F9 销售 XSDD 自动建项仅在未合并分支**：main `pipeline.py:365` 仅 MAINTENANCE；分支 diff 233 文件/+58806 → 把 SALES 建项钩子切分为独立 PR 先行合入
- **[low] F7 lifecycle_status 列快照与动态口径双轨**：动态读取点仅两处，按列筛选会引入日切漂移 → 读侧逐步全切 `lifecycle_case`
- **[low] F8 行数骤减防呆已撤销**：`master_workbook.py:4275-4277`（2026-08-22 拍板撤销，由前端弹窗兜底）→ 服务端保留可配置阈值（默认关闭）

### 信号：favors_incremental

理由：身份层（XSDD 注册表 + advisory lock + DB 触发器 + 归并工具）、锁序信封、CAS/签名行集、动态 lifecycle 这些难做的基础设施都已就位且有测试覆盖。全部 high/medium 发现都是「在现有机制上加一层」型工作：口径文档同步（F1）、冲突粒度从整本降到行级（F2，数据结构已具备）、审计补点（F3）、权限配置（F4）、建项收口（F5/F9）、期限入参补齐（F6）。重写反而会丢掉 08-27 以来用事故换来的全部防护。

---

## 6. 前端架构

前端是单一 React 18 + Vite + AntD 5 SPA，`nav.tsx` 是路由/菜单/权限/面包屑的单一真值源（345 行，设计良好）。取数层**完全没有状态库**（无 react-query/swr/zustand/redux），全部 31 个页面手写 `useEffect + axios`，仅 boss 域有共享 hook。最大问题是**死代码**：从 `main.tsx` 出发的引用闭包显示 136 个非测试源文件中 32 个不可达（约 6,600 行，占源码 12.6%），且至少 11 个死组件各带独立测试文件（约 3,200 行测试在测死代码）——这是 2026-08-16「22 页收敛为 2 页」改版后未清理的残骸。测试套件（69 文件 / 17,471 行，占全前端 33%）为行为式断言、无快照、mock 只打在 API 模块边界，质量高。`api.ts`（830 行，老域接口 + axios 实例）与 `api/` 目录（22 个新域模块）并存属时间性分工而非冲突；`version.ts`（673 行）实为 changelog 数据。

### 指标

- src 文件数 / 总行数：206 / 52,205；非测试源文件 136，可达 104，死文件 32（~6,600 行）
- 测试文件 69 / 17,471 行；状态库 0 个；手写 useEffect 取数页面 31 个
- 直接解析 `localStorage("permissions")` 的文件 10 个；读 `"role"` 的 18 个
- 金额格式化实现 ≥10 份（`utils/format.ts` 自称单一真值源但 16 文件含 `¥` 自写）
- >600 行非测试组件 9 个（含 2 个死文件）；运行时依赖 8 个全部有引用；旧维保路径兼容重定向 23 条（`nav.tsx:305-320`）

### 发现

- **[high] 大规模死代码：32 个文件约 6,600 行不可达，集中于维保旧版组件**
  - 证据：`components/maintenance/` 下 15 个死组件（`BadReturnPanel.tsx` 1,169 行、`SiteIssueWorkflowPanel.tsx` 759、`CollectionPlanImportModal.tsx` 461 等）；整个 `components/maintenance/boss/` 7 个组件全死；死 API 模块 5 个（`maintenanceCollectionReminders.ts` 399 行等）
  - 影响：12.6% 源码是噪音；死组件引用 API 模块形成假依赖链
  - 渐进修法：按闭包清单整批删除（先组件后孤儿 API 模块），删后跑 vitest 验证
- **[high] 至少 11 个测试文件（约 3,200 行）在测试死组件**
  - 证据：`__tests__/BadReturnPanel.test.tsx`（806 行）、`CollectionPlanImportModal.test.tsx`（501）、`SiteIssueWorkflowPanel.test.tsx`（381）等，对应源文件均在死代码清单
  - 渐进修法：随死代码同批删除，删前对照后端确认功能确已下线
- **[medium] 权限判断无统一入口：≥5 套机制并存**
  - 证据：`App.tsx:12`、`nav.tsx:30`、`maintenancePermissions.ts`（219 行，其中多个 capability 只被死组件消费）、`pages/boss/shared.tsx:205`、`PartSearchPage.tsx:103`；每处重复 `JSON.parse(localStorage)` + try/catch，admin 短路规则各处不一致
  - 渐进修法：抽 `usePermissions()` hook 统一；维保 capability 随死代码删除先砍一半
- **[medium] 取数层裸手写**：0 个状态库引用；`useGuardedFetch` 仅 boss 域 8 个活文件用；25 个文件各写竞态守卫 → 先把 useGuardedFetch 提升为全局 hook 逐页替换，热点页面再按需引缓存层
- **[medium] API 层双轨 + 分层倒置**：`api/maintenanceOperations.ts`（1,666 行、135 export，全库最大）反向 import UI 层的 `maintenancePermissions`（:2-4）→ api.ts 业务接口按域迁到 api/，权限读取移出 api 层
- **[medium] 9 个 >600 行巨型页面组件**：`ReplenishmentBetaPage.tsx` 1,203、`ChatPage.tsx` 896、`PoolManagementPage.tsx` 884、`MaintenanceBatchTransferButton.tsx` 769（一个"按钮"769 行）等 → 仅在需要改动的页面顺手拆
- **[medium] 金额格式化约 10 份实现**：`format.ts:1` 自称已收编，但 `pages/purchases/shared.tsx:44`、`boss/shared.tsx:196`、`PoolManagementPage.tsx:36` 等仍自写 → 以 format.ts 为基准逐页替换，2-3 个 PR
- **[low] 维保"三代同堂"遗留**：23 条重定向注释称旧页面已删除但实际未删；beta 旗标残迹 → 删死代码后清理
- **[low] version.ts 673 行 changelog 混在源码**：每次发版触发全量重新构建 → 可迁 JSON，优先级最低
- **[low]（正向）测试行为式、无快照、依赖零浪费**：0 个 `toMatchSnapshot`；8 个运行时依赖全部有实际 import

### 信号：favors_incremental

理由：骨架健康（nav.tsx 单一真值源、路由级懒加载 + manualChunks、API 按域模块化、69 个行为式测试、依赖零浪费）。三个主要病灶——死代码（纯删除）、格式化/权限重复（局部收编）、裸手写取数（逐页替换）——没有一项需要推翻整体结构。建议顺序：① 删 32 死文件 + 11 死测试；② 收编权限 hook 与金额 formatter；③ 巨型页面按需拆分；④ 视意愿在热点页引入 react-query。

---

## 7. 依赖与构建/部署链

依赖侧整体健康：后端 15 个生产依赖全部有真实引用（无零引用包），且有 `uv.lock` + `requirements.lock`（1213 个 sha256 hash）双锁链，生产镜像经 `pip install --require-hashes` 安装。主要风险集中在发布链：`.deploy/` 下并存 v120–v123 四代发布状态机（合计约 22k 行脚本），当前 v123 发布脚本（211 行）**没有任何 git 分支/SHA/工作区校验、没有部署前测试闸门、没有前后端版本一致性校验**——前代 v122 build 脚本曾有这些校验，v123 系列把 build 环节退化为发布计划文档里的一行手工命令，「fix 分支直部署」在脚本层面畅通无阻。另发现 `starlette>=1.3.1` 声明超前于 fastapi 0.136.1 官方约束（上游只要求 `>=0.46.0`），存在主版本兼容风险。

### 指标

- 后端生产依赖 15 个（`pyproject.toml:6-23`），全部有引用；lock：`uv.lock` + `requirements.lock`（1213 hash）
- pyproject 仅 2 个 `==`（fastapi 0.136.1、xlrd 2.0.2），其余 `>=`；lock 全钉
- import 计数：fastapi 71 / sqlalchemy 390 / openpyxl 64 / pydantic 30 / pandas 4（全在 etl/）/ openai 3（惰性）/ pdfplumber 1（惰性）/ docx 1（惰性）/ xlrd 1 / PIL 1
- 前端 10 个 deps 全部有引用（最少 react-markdown/remark-gfm/react-resizable 各 1）；`package-lock.json` 存在，CI `npm ci`
- fastapi 0.136.1 官方 starlette 约束 `>=0.46.0`（PyPI 核实）；starlette 实际解析 1.3.1（发布晚于 fastapi 该版本，未经上游兼容测试）
- 发布路径 ≥5 条：v120/v121/v122/v123 系列 + DEPLOY.md 手工路径 + compose 直起；CI 仅测试不部署
- v123 发布脚本护栏：git 校验 0 / 分支校验 0 / 工作区干净校验 0 / 测试调用 0（有备份 :119-129、有迁移 :131-148）

### 发现

- **[high] 当前 v123 发布链无分支/SHA/工作区护栏，「fix 分支直部署」零拦截**
  - 证据：`.deploy/v123_maintenance_boss_release.sh`（211 行）全文无任何 `git` 命令；preflight 只校验发布包 hash（:108-117）。对比前代 `v122_collection_reminders_build.sh:27-33` 曾强制 `TARGET_SHA == origin/main` 且工作区干净；v123 没有 build 脚本，构建退化为 `docs/releases/v1.23-deploy-plan.md` §3 里的手工命令
  - 渐进修法：preflight 复用 v122 build 脚本 :27-33 的现成校验，约 15 行 bash
- **[high] 部署前无测试闸门，CI 固化 PR 号硬编码豁免**
  - 证据：v123 release 脚本无 pytest/vitest 调用；CI 只挡 PR（`.github/workflows/ci.yml:3-7`），直接部署完全绕过；`ci.yml:50-57` 对 PR #251/#252 硬编码跳过全量后端套件
  - 渐进修法：preflight 增加「TARGET_SHA 的 CI 必需检查为绿」校验（`gh run list --commit` 一行）；删除 ci.yml:50-57 的 PR 号豁免
- **[medium] `starlette>=1.3.1` 声明超前于 fastapi 官方兼容约束**
  - 证据：`pyproject.toml:8` vs fastapi 0.136.1 requires_dist `starlette>=0.46.0`；fastapi 0.136.1 发布 2026-04-23 早于 starlette 1.3.1 的 2026-06-12（`uv.lock:1554-1561`）
  - 渐进修法：`starlette>=0.46,<1.0` 重新 lock，或升级 fastapi 到官方支持 starlette 1.x 的版本
- **[medium] 前后端版本一致性无校验，版本号为静态占位**：两端 version 均为 `0.1.0` 从未递增；v123 `compose up -d app frontend`（:155）不校验两镜像同源 → Dockerfile 注入 `GIT_SHA` 为 OCI label，deploy 阶段 `docker inspect` 比对
- **[medium] 四代发布状态机并存，旧路径未退役**：`docs/DEPLOY.md:8-23` 仍把 v1.20 runbook 当现行规范；v123 与旧脚本状态机语义已不同 → 旧脚本加退役头或移入 `.deploy/archive/`；DEPLOY.md 只指向当前世代
- **[low] 重依赖可隔离为可选依赖**：pandas 4 处全在 etl/；pdfplumber/docx/openai/xlrd/PIL 各 1-3 处且已惰性 import → pyproject 加 extras，不急
- **[low] 依赖声明以 `>=` 为主，非 lock 路径会漂移**：`pip install -e .` 会拉到最新版本（starlette 正是这样被抬到 1.3.1）→ 开发统一 `uv sync --frozen` 或收紧为 `~=`
- **[low]（信息性）python-multipart 零直接 import 但运行时必需**（UploadFile/Form 39 处），防止误删

最小护栏清单（按优先级）：① v123 preflight 加 HEAD==origin/main + 工作区干净校验（15 行，抄 v122）；② preflight 加目标 SHA 的 CI 绿校验（1 行 gh 命令）；③ 删除 ci.yml PR 号豁免；④ 两端镜像注入 GIT_SHA label 并比对；⑤ 旧世代脚本加退役头；⑥ CI 加 `alembic heads==1` 卡口（见维度 3）。

### 信号：favors_incremental

理由：依赖锁链是仓库里最成熟的部分之一。所有高危缺口都是「在现有 v123 状态机 preflight/deploy 阶段加 15-50 行校验 + 旧脚本加退役头」级别，且 v122 脚本里就有可复制的现成校验代码。

---

## 8. 测试与重构安全网

后端测试规模可观：243 个 `test_*.py` 文件、3229 个测试函数，以「真实 PostgreSQL + TRUNCATE 隔离 + TestClient 打 API」的行为型集成测试为主干（195 个文件用真实 `db` 会话，85 个用 TestClient），比纯 mock 套件更接近重构安全网的形态。但三个结构性问题：其一，套件硬绑定 Linux + 本机 5433 PostgreSQL，本机（macOS）连 `--collect-only` 都在 conftest 阶段抛 `RuntimeError`；其二，若干大体量核心模块测试密度严重失衡；其三，78 个测试文件用 monkeypatch，其中 81 处直接钉在私有 `_xxx` 函数上，这部分测试会抵抗重构而非保护重构。前端无 e2e。

### 指标

- 后端测试文件 243、测试函数 3229；用 TestClient 86 文件、用真实 db 夹具 195 文件、纯单元 44/243 ≈18%
- 用 monkeypatch 的文件 78 个，其中钉私有 `_xxx` 的 setattr 81 处
- 维保相关测试文件 121；前端 vitest 仅 4 个文件、无 playwright/cypress
- `pytest --collect-only` 实测失败：`RuntimeError: Linux isolation capabilities required`（`tests/run_isolation.py:178`）；本机 5433 端口关闭
- 核心模块测试密度（上限估计）：operations 7199 行→625 函数；master_workbook 5269→126；roundtrip 4784→228；**bulk_import 3126→12**；**project_identity 2197→12**；**migration_runs 1738→20**；standardize 2310→38；maintenance_cost 2349→533

### 发现

- **[high] 测试套件平台锁定：仅 Linux 可跑，本机连收集都失败**
  - 证据：`tests/run_isolation.py:167-178` 要求 `sys.platform == "linux"`；`--collect-only` 实测抛 RuntimeError；`backend/.venv/bin/python` 是 broken symlink
  - 影响：开发者无法在 macOS 本地验证重构，安全网只在 Linux CI/容器里存在
  - 渐进修法：平台门禁降级为「能力探测 + 跳过危险特性」，或官方提供 devcontainer/compose 测试入口
- **[high] 依赖外部 PostgreSQL 且默认地址与 docker-compose 不一致**
  - 证据：`conftest.py:24-26` 默认 `127.0.0.1:5433/spareparts_test`，而 `docker-compose.yml:28` 映射 5432；隔离为每进程独占建库 + TRUNCATE（`conftest.py:280-284`），禁用 xdist
  - 渐进修法：统一端口约定并文档化；中期评估事务回滚隔离以放开并行
- **[high] 大体量核心模块测试密度严重失衡**
  - 证据：`maintenance_bulk_import.py` 3126 行仅 12 函数；`maintenance_project_identity.py` 2197 行仅 12 函数（含合并、FK 反射 `_database_project_fk_catalog` 等高风险逻辑，:497）；`maintenance_migration_runs.py` 1738 行仅 20 函数
  - 影响：这些恰是「数据改错难恢复」的导入/身份合并/迁移模块，重构时无网可兜
  - 渐进修法：优先按公共入口（`resolve_xsdd_project`/`claim_xsdd_project`）补行为测试
- **[medium] 81 处 monkeypatch 钉在私有函数上，属「钉死实现」**
  - 证据：`test_maintenance_roundtrip.py:143-157` 钉 `_load_and_parse`；`test_maintenance_bulk_import.py:165-166` 钉 `_all_contracts_by_order`；`test_maintenance_cost.py:509-520` 钉 `maintenance_boss_board._card_contracts`
  - 影响：重命名/拆分私有函数即红，即使行为不变——产生「重构恐惧」
  - 渐进修法：逐步改写为经公共入口 + 真实 db 的行为断言；mock 仅限真外部边界
- **[medium] 测试类型比例：集成主导，纯单元约 18%，前端 e2e 缺失**：纯算法层（日期口径、金额 rounding、standardize 规则）单元测试偏薄 → 纯函数模块补快单元测试（不依赖 PG，同时缓解本机可跑性）
- **[low]（正面）主干测试是行为型集成测试**：`test_maintenance_project_operations_api.py:56-119` 走 TestClient 登录→打 API→断言响应；conftest 用真实 alembic 迁移建 schema 而非 sqlite 替身
- **[low] 自研隔离基础设施单点脆弱**：`run_isolation.py` 全文 1142 行，自身未测，出问题全盘皆红 → 接受现状，避免叠加更多自研机制

结论：对 operations/cost/roundtrip/boss_board/replenishment/source_assignments —— 现有测试可作渐进重构安全网。最该补测试的模块（优先级序）：① `maintenance_bulk_import.py`；② `maintenance_project_identity.py`；③ `maintenance_migration_runs.py`；④ `standardize.py`（纯函数多、补测成本最低）；⑤ `maintenance_project_master_workbook.py`（补改签/导出边界用例）。

### 信号：favors_incremental

理由：核心高流量模块已有 3000+ 以真实库 + API 为主的行为测试，这是重写项目几乎不会具备的资产；缺口集中在可枚举的 4-5 个模块，属「定点补网」而非「全网重建」。

---

## 9. agent 协作治理与共享记忆

仓库存在约 20 个「给 agent 看」的指令/上下文文件，分属 Claude（CLAUDE.md、.claude/、.ai/）、Codex（主仓未入库的 AGENTS.md）、zcode（.zcode/plans/）与工具中立区（CONTEXT.md、docs/adr/、docs/agents/），**没有任何一个文件被全部三个工具共同读取**。业务口径的「单一事实来源」在形式上有雏形（CONTEXT.md 术语表 + docs/adr/ + product-decisions.md），但三处关键口径互相矛盾、revision 推进这一事故核心机制根本无决策记录。更严重的是事故模式在调查当天仍在重演：2026-09-02 拍板的「负责人/销售全量编辑权」只存在于一个**未入库的 zcode 会话 plan 文件**里。工具归因也断裂：617 个提交中 zcode 零归因、codex 只能靠分支名辨认。

### 指标

- agent 指令/上下文文件 ~20 个；`.ai/AI_WORKFLOW.md` 引用 10 个文件全部 MISSING
- 未入库的关键决策载体 3 个：主仓 AGENTS.md（`??`）、`.zcode/plans/plan-sess_f45a35e1…`、`outputs/` 事故报告
- 「缺行口径」互相矛盾的文档 4 处（CONTEXT.md / workbook-template-design / delete-void 契约 / 代码 fb7e24b）
- 提交归因（`git log --all` 共 617）：Claude trailer ~197；grep codex 18；**zcode 0**；codex 分支 17 个全部落在 maintenance 域
- ADR 4 篇均有日期+状态，但编号冲突 1 处、无 superseded 机制
- `.ai/CHANGELOG.md` 最后更新 2026-08-19，其后 maintenance workbook 20+ 提交零留痕

### 发现

- **[high] 主仓 AGENTS.md 未入库，跨 worktree 不可见**
  - 证据：`git -C /Users/yangjinchen/Code/IT_data status --short AGENTS.md` → `??`；本 worktree 无此文件
  - 影响：Codex 的 233 行工作协议只在那台机器那一个 checkout 生效
  - 渐进修法：一次性——AGENTS.md 提交到仓库根，CLAUDE.md 加指针
- **[high] `.ai/AI_WORKFLOW.md` 是僵尸协议，引用 10 个不存在文件**
  - 证据：`.ai/AI_WORKFLOW.md:15-20,114-117,133` 引用全部 MISSING；其硬性留痕条款（:117）与 `.ai/CHANGELOG.md` 停在 08-19 矛盾
  - 渐进修法：一次性——删除或降级为历史参考，有效条款并入 AGENTS.md
- **[high] 「缺行/删行」口径四处互相矛盾，正是事故级歧义**
  - 证据：`CONTEXT.md:134`「缺行不代表删除」（blame 07-29）；`docs/maintenance/workbook-template-design.md:23`「缺行 ≠ 删除」（08-17）；`docs/maintenance/contracts/project-master-delete-void.md:44`「**缺行 = 作废**」（08-20）；代码 `fb7e24b`（08-23）执行缺行=作废。CONTEXT.md 在 08-25 被编辑过却没同步
  - 渐进修法：一次性——以 delete-void 契约为准改 CONTEXT.md:134；防腐靠决策日志+CI
- **[high] revision 推进范围（事故机制核心）无任何决策记录**
  - 证据：`3036183` 把 `etl/loader.py` 与 `maintenance_demands.py` 接入 bump，但 ADR/product-decisions/CONTEXT.md 均无条目；该口径首次成文在未入库的事故报告里
  - 渐进修法：一次性——补一篇 ADR《工作簿 revision 推进范围》列全部 bump 点；CI grep 校验 bump 调用点数与 ADR 清单一致
- **[high] 事故模式当天重演：09-02 新决策仍只存在未入库的 zcode plan**
  - 证据：`.zcode/plans/plan-sess_f45a35e1-…md:3`「项目负责人/销售对本人项目工作簿全部字段可见可改（2026-09-02 业务决策）」——该文件 untracked；codex 在分支上实现它（`2764967`/`e9ecc52`）
  - 渐进修法：plan 中「已拍板」段落必须同步进决策日志后才允许开 PR；PR 模板必填决策条目号
- **[medium] 工具归因断裂：zcode 零痕迹、codex 仅分支名可辨**
  - 证据：617 提交中 grep zcode=0；作者全部收敛为同一人；`maintenance_project_master_workbook.py` 被三类工具反复改写（`fb7e24b`/`3036183`/`2764967`）
  - 渐进修法：约定各工具提交必须带 trailer；pre-push hook 或 CI 校验（比纯自觉持久）
- **[medium] ADR 机制有雏形但编号冲突、覆盖域窄、无 supersede 路径**：两个 0002（同提交 `caf4a97` 引入）；工作簿协议/revision/权限/税口径均无 ADR → 重编号；模板加 `supersedes` 字段
- **[medium] `product-decisions.md` 范围错位、无逐条状态**：15 条全是补库域；事故三要素核对——XSDD 第一源 ✓（CONTEXT.md:45-47）、工作簿全量修改 ✗（矛盾）、revision 推进 ✗（无）→ 迁移为 `docs/decisions/` 统一决策日志
- **[medium] 多工具反复改写同一模块且无接口契约守护**：master_workbook 并发语义 35 天内被反转两次（`fb7e24b` 加缺行=作废 → `3036183` 整本 CAS → `2764967` 行级三路合并取代整本作废）→ CI 口径守护（见方案 d）
- **[low] `docs/agents/domain.md:16-19` 自述「this repo doesn't have CONTEXT.md or docs/adr/ yet」**——两者都存在 → 删过时段落

跨工具共享记忆最小方案：**a) 通用入口**（一次性）：根目录提交单个 AGENTS.md，CLAUDE.md 改为指针文件，zcode/codex 配置指向同一文件。**b) 决策日志**（一次性建设+轻纪律）：`docs/decisions/NNNN-<slug>.md`，字段固定（日期/状态/口径一句话/影响代码锚点/拍板人），首批回填 4 条（XSDD 即身份、缺行=作废适用表清单、revision bump 点清单、09-02 权限下放）；supersede 只能新增不能改旧文件——git 本身即防腐。**c) PR 引用决策条目**（纪律维持，会腐烂）：PR 模板必填 `决策: D-NNNN 或 "无相关决策"`，防腐靠 CI 校验该行存在（把纪律降级为格式检查）。**d) CI 口径守护**（一次性，建成后自动运转）：每个决策条目配可机检锚点，如 grep `bump_locked_workbook_revision` 调用点数与决策文件清单比对，不一致即红。

### 信号：favors_incremental

理由：问题全部在治理与文档层，不涉及代码架构缺陷。「提交 AGENTS.md + 建 docs/decisions/ + 两条 CI 检查」1-2 天可收口。仓库已有良好底子（ADR 四段式格式、CONTEXT.md 术语表带 _Avoid_、delete-void 契约精确到表级）——团队能写出高质量决策文档，缺的是跨工具唯一入口和防腐机制。

---

## 10. git 历史与变更模式

仓库 2026-05-25 建仓至今 497 个提交（origin/main，tip `c9d67fe`），其中 **282 个（57%）集中在 2026-08 一个月**。fix:feat 比值从 7 月的 0.65 恶化到 8 月的 2.08（近30天 2.03），修复类提交占比近30天达 44%，呈典型救火曲线。「双头基线」属实且比预期更严重：GitHub main 在 `b58dded`（08-26 15:51）冻结了 **6.4 天**，期间 29 个提交全部堆积在侧分支 `codex/prod-maint-repair-20260828` 上，09-02 01:38 才通过 `bf15a36` 一次性并回。全仓 0 个 revert 提交——所有纠错都是前向修复。维护者实为 1 人（bus factor = 1）。

### 指标

| 指标 | 全量（497 commits） | 近30天（290 commits） |
|---|---|---|
| fix | 143（28.8%） | 128（44.1%） |
| feat | 86（17.3%） | 63（21.7%） |
| test | 25 | 21 |
| merge | 60（12.1%） | 30（10.3%） |
| revert | **0** | **0** |
| fix:feat | 1.66 | **2.03** |

- 月度：6 月 106 → 7 月 91（fix:feat 0.65）→ 8 月 282（fix:feat **2.08**）→ 9 月 5
- 作者：云间辞 243 + Jinchen-Yang 253（同一人）；Co-authored-by 全量 121（全部 Claude）、近30天仅 12；codex 标记全量 15/近30天 14；zcode/kimi = 0
- 分支前缀：codex/ 10、fix/ 5、hotfix/ 3、feat/ 3（共 43 分支）
- main 第一父链空洞：`b58dded`(08-26 15:51) → `bf15a36`(09-02 01:38)，间隔 6.4 天、0 提交
- 未并入 main 的分支提交：sales-xsdd-auto-project 22、master-workbook-full-edit 5、其余 4 个分支各 1-2
- 巨头文件行数（07-01 → 08-01 → 09-02）：operations 0→0→**7199**（08-08 创建，25 天 7.2k 行）；master_workbook 0→0→**5269**（08-17 创建）；roundtrip 0→4762→4784；permissions 121→541→**1087**；etl/loader 549→765→**1381**
- 热点 top5（07-01 起）：version.ts 61 次、operations.py 54、其 API 测试 37、master_workbook.py 31、nav.tsx 25；30 天内改动 ≥10 次的文件 32 个（几乎全是维保模块 + 测试 + v122 发布脚本 15 次）

### 发现

- **[high] 「双头基线」属实：main 冻结 6.4 天，生产跑侧分支**
  - 证据：main 第一父链 `b58dded`(08-26) → `bf15a36`(09-02) 中间 0 提交；侧分支 `codex/prod-maint-repair-20260828` 堆积 29 个提交（08-31 单日 11 个）
  - 影响：任何从 main 拉代码的人拿到落后生产 7 天的基线——09-02 事故的直接成因
  - 渐进修法：侧分支修复当日回 main；加「侧分支不得领先超过 24h」检查
- **[high] GitHub PR 被合并到侧分支而非 main**
  - 证据：`0a8a022`「Merge pull request #293」(08-29) 的父提交在侧分支上，不在当时的 main 上；`27da3d6`(#296) 同样
  - 影响：PR 保护形同虚设——合并目标是可以直推的 codex 分支
  - 渐进修法：默认分支保护 + PR base 限定 main；codex/* 禁止作为 PR base
- **[high] 修复速率失控：8 月 282 提交占全史 57%，fix:feat 0.65 → 2.08**
  - 证据：08-23 单日 14 提交中「报销归因」一小时内三连修同一回填逻辑（`ee2f800`/`ea9e6fd`/`7b74771`）；test 21/290 远跟不上修复速度
  - 渐进修法：维保回填/导入链路设「修复冷静期」——同类根因第二次出现时先补回归测试再改
- **[high] 巨头服务文件爆炸式增长，热点与巨头完全重叠**
  - 证据：operations.py 创建 25 天到 7199 行且 30 天改 54 次（全仓第二热）；permissions.py 两个月 9 倍
  - 渐进修法：按子域拆成 <1500 行模块，拆分时只搬不改（与维度 2 方案一致）
- **[medium] 孤儿分支遗留 33 个未合并提交**：sales-xsdd-auto-project 22 个、master-workbook-full-edit 5 个等；export 分支内容已以别的哈希落入 main，原提交成孤儿 → 逐个比对后删除或合并；建立周例行清理
- **[medium] 零 revert 文化**：497 提交无回滚，出错只能前向堆修复（与发现 3 互为因果）；08-26 还出现过迁移级双头修复 `a67d287` → 发布脚本固化 revert + 镜像回滚预案
- **[low] AI 工具主力从 Claude 切到 Codex，提交规范同步漂移**：近30天 Co-authored 仅 12 个（vs 全量 121）；6 月 106 个提交完全不用 conventional 前缀 → commit template 或 CI 强制前缀 + AI 署名 trailer
- **[low] Bus factor = 1**：495/497 提交来自同一人；无 review 缓冲 → main 强制 CI 绿灯 + 至少 bot 审查门槛

### 信号：favors_incremental

理由：问题高度局部化——热点 30 个文件几乎全落在维保模块及其测试与发布脚本上，其余代码变更平缓。救火循环的驱动力是流程失序（侧分支部署、PR 打偏、无回滚）加少数上帝文件，而不是整体架构不可救——7 月数据（fix:feat 0.65、提交规范齐整）证明同一套代码库可以健康演进。对单人维护、正在服务生产的系统做重写，风险远大于「拆 operations.py + 恢复 main 分支纪律 + 补 revert 预案」这三件增量的事。

---

## 遗留问题（需要业务方拍板的）

1. **缺行语义**：代码实际执行「删行=作废」（02-06 五张表，2026-08-20/23 拍板记录），CONTEXT.md 写的是「缺行不代表删除」。请确认：是否五张表都适用缺行=作废？是否需要在服务端恢复「单表缺行超 N 行需二次确认」的防呆阈值（目前只靠前端弹窗兜底）？
2. **冲突处理取向**：工作簿冲突目前是「整本 CAS、不符即 409」。是否接受改为「行级冲突校验（只校验用户改过的行）+ 强制接管（带审计的覆盖）」？强制接管的权限应给谁（仅管理员，还是项目负责人也可）？
3. **全量修改权限下放**：2026-09-02 决策「项目负责人/销售对本人项目工作簿全部字段可见可改（含成本与合同额）」目前只存在于未入库的 zcode plan。请正式确认该决策及范围——特别是「含成本与合同额」是否适用于所有销售，还是仅限项目负责人。
4. **销售订单导入自动建项**：`codex/sales-xsdd-auto-project` 分支（233 文件/+58806 行）实现了「销售导入→建维保项目」，未合并。该能力是否仍是当前需求？若是，是否接受将其切分为独立小 PR 先行合入（而非整体合并 5.8 万行）？
5. **手工建项的期限口径**：手工创建项目目前无期限录入入口且硬编码「期限缺失」。期限（起止日期）是否应成为建项必填/选填字段？对确实无期限信息的项目，业务上希望它们落在哪个桶？
6. **非标准合同号的身份规则**：身份链对标准 XSDD 格式合同号有数据库级唯一保障，非标准格式合同号可挂多个项目。业务上是否存在合法的非标准合同号？它们应遵守什么唯一性规则？
7. **权限模型收敛**：是否接受把「45 键逐账号勾选」收敛为「角色 × 模块 × 读/写/敏感数据」矩阵（守卫机制不变，约 3-5 人日）？admin/boss 的行级「全部项目可见」是否允许被权限中心收紧？
8. **lifecycle 词表**：存储列写 `active`、过滤链路认 `ongoing`，两者永不匹配。统一为哪一个词？（涉及存量数据回填方向）
