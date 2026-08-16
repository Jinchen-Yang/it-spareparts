# 维保备件展示板 v1.3 全栈执行计划（合并版，2026-08-16）

> **本文 = 后端 v1.2 执行计划 + 前端信息架构设计 + 业务确认的 Q1–Q4 决议，合并为一份
> 交给实现者（Claude）直接执行的单一计划。** 两个上游：
> - 后端：`docs/maintenance/archive/plan-v1.2-executable.md`（949 行，字段级契约，已归档）
> - 前端：`docs/maintenance/frontend-information-design.md`（454 行，信息四层/状态语义/归属唯一，附录 A 全文收录）
> - 事实与需求：业务口径以两本活文档为准——`docs/maintenance/ARCHITECTURE.md`（业务架构）、
>   `docs/maintenance/REQUIREMENTS.md`（核心需求增量表）；事实档案已归档至
>   `docs/maintenance/archive/data-facts-and-linkage-2026-08.md`。
>
> 合并决议（Q1–Q4 业务已确认，附录 B）：
> - Q1 指标三槽 = 回款率 / 已知申请估算成本(占合同，不完整前缀 ≥) / 待办件数；返还率只作状态位与事实行，不占槽；
> - Q2 「阻塞」类只提示 + 「复制描述」按钮，不建转派流程（后事实展示板，铁律 1）；
> - Q3 决断条优先级 = 截止日最早；金额仅随行展示；
> - Q4 `not_counted` 归「不适用」（灰点半透明无外框），但事实层/依据层保留
>   「该合同未计入总额（台账 included_in_total=false）」一句；若业务日后发现需人工干预，
>   升级为待办（M0 追认项 F6 备案）。
>
> 相对 v1.2 的合并改动：铁律增加第 8 条（前端设计系统）；里程碑增加 M3b（前端页面）；
> §5 前端章节替换为合并版；附录 A/B 为前端设计与合并决议原文。

> 本版以 v1.1（`docs/maintenance/archive/plan-v1-boss-dashboard.md`）为底稿，修正其中的错误与粗糙处，
> 不改变方向。全部数据引用注明出处：「事实档案」= `docs/maintenance/archive/data-facts-and-linkage-2026-08.md`，
> 「需求定义」= `docs/maintenance/archive/甲方核心需求与问题定义.md`（已归档，活口径见 ARCHITECTURE/REQUIREMENTS）。代码引用均已在当前分支
> `feat/maintenance-ledger-import`（基线 main = 生产 SHA `4f8b6881`）逐一核实。
>
> **本文只是计划，不含任何代码改动。**
>
> 相对 v1.1 的主要修正（详见各节【v1.1 修正】标注）：
> 1. 「重复业务名放行」前提不成立——两份真实导出均无精确重复列名（事实档案 §1.1），
>    防御策略改为「按段解析＋同段重名才 fail-closed」；
> 2. 「最小迁移链＝只提取所需迁移」不可执行——分支迁移是**线性链 12 个修订**，无法安全抽取，
>    改为「整链发布（工程推荐）＋逐修订加法审计＋冻结功能三重封存」，并将其与铁律 7 的张力
>    显式上交为 M0-E 签署项（§2.6 M5-1、§7.1）；
> 3. 34/28 新增列此前只有名字，本版补齐 类型/源字段/说明 完整 DDL，并显式列出 8 个不落库列（§3）；
> 4. 补齐 v1.1 缺失的 API 字段级契约、HTTP 权限矩阵、六种空值状态信封、测试文件清单、风险表；
> 5. 工期合计修正：v1.1 写 29–40，按其分项相加实为 **28–37** 人日（§1）。

---

## 0. 定位与铁律（不许推翻）

**定位**：给老板做决策用的**后事实展示板**——定期上传 → 归集 → 成本回填 → 展示；
不做业务流程闭环（需求定义 §1）。v1 展示重点=「项目成本和备件的申请」（需求定义 §3.5 甲方补充）。

铁律（与任务书一致，逐条落到本计划的执行位置）：

| # | 铁律 | 本计划落点 |
|---|---|---|
| 1 | 后事实展示板，不做流程闭环 | 全文；无任何审批/工单/回写氚云能力 |
| 2 | 成本回填引擎已存在（取价瀑布，生产覆盖 88.7%，事实档案 §3），**只接线不重写**；口径=采购价优先→无采购用销售价→时间就近 | §2.2 M1-8：复用 `maintenance_cost.recompute`（`backend/app/services/maintenance_cost.py:341`），不改引擎一行 |
| 3 | 需求单流转状态列（已采/待供/待返/领用等）只原样展示、不计算、不标注 | §3 明细 28 列全部 nullable 落库；§2.5 M3-1 聚合服务列白名单排除全部状态列；证据下钻表原样显示；头级自报列仅**无判定并排展示**（M4-4，系统不做差异计算、不打差异标注） |
| 4 | 预交付项目并入真实合同展示（不叫合并） | §2.3 M2-3：归属候选按 `project_std`（已剥前缀）指向真实项目，单据级「预交付」徽标取自 `project_raw` 前缀 |
| 5 | 事实源分离：实发=CKD 发货单、未用件收回=return_order 返库单(成品)、坏件回收=rkd_inbound 入库单(坏品/废品，返件类)；各源带 as_of/readiness，未导入显示 not_imported，绝不显示 0 | §2.4 M4-1/M4-2；§4.4 /health 契约 |
| 6 | 权限=勾选名单制；上传用 maintenance-only 专用端点＋专用动作键，不复用 `/api/import/upload` | §2.2 M1-6：新端点 `POST /api/maintenance/wbdd-imports` ＋ 新键 `action_maintenance_wbdd_import`（注：现有全家桶端点实际路径是 **`/api/import/upload`**，router prefix=`/import`，`backend/app/api/imports.py:30`，v1.1 写作 `/api/imports/upload` 有误） |
| 8 | 前端设计系统（新旧维保页面统一，附录 A）：信息四层（决断/指标/事实/依据，一页一条决断层）· 状态语义六类（仅逾期/待办用暖色）· 归属唯一（每事实只在一处）· 项目卡≤3 状态位 | §5 全部页面 |
| 7 | 发布走最小迁移链（纯加法 nullable）＋独立 feature flag，回滚=关 flag，不做 downgrade 回滚 | §7：**本计划新增迁移仅 2 个且严格纯加法 nullable**。但发布链还须携带分支既有 12 个线性修订（其中 2 处非纯加法点，线性链无法抽取——§2.6 M5-1）；该现实与铁律 7 字面存在张力，列为 **M0-E 发布链口径**由铁律 owner 书面签署，本计划不单方面改判。flag `maintenance_boss_dashboard_enabled` 默认 false |

**冻结清单（本计划不得出现，代码保留、导航隐藏、不上线）**：AI 兜底列映射、补库购物车 Beta、
回款凭证上传、坏件变卖登记、项目工作簿 v3 导出、前置库账本页、收回清单页、销售看板页、报销对账页
（需求定义 §3.6）。§7.1 给出冻结功能三重封存核对表。

**基线数字**（引用出处随行标注）：

| 指标 | 值 | 出处 |
|---|---|---|
| 生产需求单 / 明细行 | 19,046 单 / 38,483 行（2023-07-03 ~ 2026-08-14） | 事实档案 §3 |
| 2026-01-01 至今 | 6,903 单 / 14,272 行 | 事实档案 §3 |
| 2026-08-16 新导出 | 6,913 单头 / 6,633 明细行 | v1.1 §1 基线表（事实档案未收录该行数，实现时以真实文件复核；6,913 vs 生产 6,903 差 8-15/8-16 两天增量见事实档案 §3） |
| 成本覆盖 | 88.7%（34,138/38,483） | 事实档案 §3 |
| 成本来源分布 | direct 11,600 / window 8,807 / pool_purchase 4,813 / none 3,461 / month_avg 3,448 / purchase_history 2,818 / sales_history 2,628 / 空 884 / pool_sales 24 | 事实档案 §3 |
| 发货单↔需求单命中 | 99.8%（5,701/5,712 维保供货头，按 WBDD 单号）；发货单**无项目名列** | 事实档案 §2.1 |
| 返库单↔需求单 | 宽版按单号 79%（760/962）、窄版 100%；宽版 3,019 单头/7,102 明细；测试结果 成品 6,703/坏品 399；返库单**有项目名列** | 事实档案 §2.2 |
| 通用池命中（2026） | 行级 50.4%（7,187/14,272）、PN 级 21.5%（619/2,883）；active 池占 98.6%，archived 1.4%（101 行） | 事实档案 §3.1 |
| 项目 | 生产 maintenance_project 415 个（lifecycle_status 全 missing，台账未导入生产）；2026 新导出 226 个项目名（YTD 有单） | 事实档案 §3；226 来自 v1.1 §1/复审记录 |
| 预交付 | project_raw/std 带「预交付-」9,291 条；2026 新导出 38 个预交付项目名 | 事实档案 §3 |
| 状态列不可信证据 | 「领用数量」29,140 行中仅 18 行非空 | 事实档案 §1.3 |
| 生产缺口 | 领用/返还/坏件/回款 = 0 行 | 事实档案 §3 |

---

## 1. 里程碑总览（M0→M1→M2→M4→M3→M5）

| 顺序 | 里程碑 | 内容一句话 | 人日 | 前置 |
|---|---|---|---|---|
| 1 | **M0 口径确认单** | 四项待拍板书面确认＋默认值确认 | 1–2 | 无 |
| 2 | **M1 WBDD 数据底座** | 90/91 双布局导入、34/28 补全列、专用端点、成本接线、精确对账 | 6–8 | M0 可并行（M0-C/D 不阻塞 M1） |
| 3 | **M2 稳定项目归属** | 候选生成＋人工确认＋未归属桶＋预交付挂靠 | 4–5 | M1 |
| 4 | **M4 正确事实接线** | 三源（CKD/return_order/RKD）readiness/as_of＋项目+PN 聚合＋顺序无关 | 5–6 | M1＋M0-D（默认粒度可先行开发，**合入前须书面追认**）；M2 并行推进 |
| 5 | **M3 决策看板** | 首屏五段＋分页列表＋证据下钻＋权限矩阵（**M0 拍板后才开工**） | 8–10 | M0 全部＋M1/M2/M4 |
| 6 | **M3b 前端设计系统与页面** | boss 五页（§5.1）+ 既有维保三页重构（§5.2，附录 A 规则）+ 导航角标 | 6–8 | M0 Q1–Q4＋M3 后端端点 |
| 7 | **M5 灰度与生产闸门** | v123 发布链、迁移审计、演练、灰度、回滚 | 4–6 | 全部 |
| | **合计** | | **34–45**（两名工程师并行＋QA/业务验收同步，约 4–5 自然周） | |

【v1.1 修正】v1.1 §9 合计写 29–40，按其自身分项（1-2/6-8/4-5/5-6/8-10/4-6）相加应为 28–37。

**新增 schema 一览（本计划新增仅 2 个迁移，均纯加法）**：
M1 一个列迁移（f_maintenance_order +34 列、f_maintenance_line +28 列，全 nullable、无索引变更）＋
一个权限键回填迁移（`page_maintenance_boss`、`action_maintenance_wbdd_import` 默认 false）。
M2/M3/M4 **零 schema 变更**。发布另携带分支既有 12 个线性修订
（见 §2.6 M5-1 审计与 M0-E 签署项——发布链≠本计划新增迁移，两个口径全文严格区分）。

---

## 2. 各里程碑详细任务

### 2.1 M0 口径确认单（第 0 周，纯业务沟通，1–2 人日）

产出物：`docs/maintenance/M0-口径确认单.md`（业务签字/书面确认），逐项记录选项与结论。
**不替业务拍板**，本计划只列任务与选项：

| 项 | 待确认问题 | 选项（不预设结论） | 阻塞范围 |
|---|---|---|---|
| **M0-A** | 老板每周/月据此**具体做什么决定** | 决定「需关注事项」队列内容。候选（前 5 项 v1.1 §6.1）：本期超预算项目 / 归档池件仍在流转（事实档案 §3.1：archived 池命中 101 行）/ 待返件多 / 无参照价占比高 / 未归属单；第 6 项「快照差异单」为本版新增（来自 M1-7） | 阻塞 M3-6 |
| **M0-B** | 项目经理看**本人项目还是全部** | ①全部可见（需求定义 §2 当前倾向）②仅本人项目（走 `resolve_visible_project_ids` 范围）③无第二类账号 | 阻塞 M3-3 矩阵第二列；权限设计已两案兼容（§6.2） |
| **M0-C** | 「已知申请估算成本（含税）」与「项目已计成本」的**正式名称与展示位置** | API 字段键固定为中性英文键（§4.3）；界面文案随 M0-C 结论定；§4.5/§5 的卡位与列位仅为**默认建议布局，业务可否决重排**（不视为已拍板） | 阻塞 M3 文案与卡位终稿，不阻塞后端 |
| **M0-D** | 事实粒度 | v1 建议**项目＋PN 聚合**（复审核验：CKD/RKD 无需求明细行级键，需求单重复 PN 无法行级分配——事实档案 §4）；备选：明细行级分配（v1 不做，如业务坚持则重估 M4 工期） | 阻塞 M4-2 合入（按建议默认可先行开发，**合入前须书面追认**；未追认不发布） |
| **M0-E** | **发布迁移链口径**（铁律 7 张力，见 §2.6 M5-1） | 分支既有 12 修订为线性链、无法抽取，其中 `e7b3d9f2c1a4`（3×drop_constraint＋7×execute 回填）、`c3b5d9e1f7a2`（alter_column＋drop_constraint）非纯加法。选项：①整链发布＋逐修订审计＋旧应用兼容演练（工程推荐）②重写迁移链只留所需修订（高风险，弃已验证迁移与测试）③先拆分冻结功能另行发布（工期大增）。**须铁律 owner 书面签署，本计划不预设结论** | 阻塞 M5 发布（不阻塞开发） |

**附带默认值确认（有推荐默认，未答复可先按默认推进，M3 前须书面追认）**：

| 项 | 建议默认 |
|---|---|
| F1 上传节奏与快照规则 | 全量快照 upsert（键=raw_order_id/raw_line_id）；同文件重传幂等；上批有本批无 → 只进对账报告「差异清单」，不删除不隐藏；人工作废走既有安全删除（tombstone，ADR-0003）；数据状态=已取消/作废 保留入库、原样展示、**聚合不剔除**（保证与生产母集 19,046 精确对平） |
| F2 stale 阈值 | 数据源 as_of 距今 > 45 天 → 展示态 `stale`（周/月上传节奏的 1.5 倍） |
| F3 报销/回款进看板 | v1 不进（冻结清单含报销对账页；老板五项中的报销/回款依赖 BXD 对账与台账口径，列后续版本）——需求定义 §3.5 甲方补充「首先是项目成本和备件的申请」支撑此裁剪 |
| F4 台账预交付建档口径 | 台账是否为「预交付-X」建**独立项目档案**？默认=不建（预交付只是单据前缀）→ M2-3 采用方案 B；若台账确有独立预交付档案 → 启用方案 A（追加 `attached_to_project_id` 纯加法列） |
| F5 自报差异提示 | 头级自报四列与三源事实**只并排展示，系统不计算差异、不打标注**（铁律 3 字面）；v1.1 §5-5 的「差异只提示」若业务确需，须在此处书面豁免后才实现（默认=不做） |

验收标准：确认单含 A–E 五项书面结论＋F1–F5 追认记录；M3 开工评审检查该文件存在且签署；
M4-2 合入评审检查 M0-D 追认；M5 发布闸门检查 M0-E 签署。

---

### 2.2 M1 WBDD 数据底座（第 1 周，6–8 人日）

#### M1-1 双布局探测与按段解析

- 改动文件：`backend/app/etl/transform.py`（`_transform_maintenance`，L204 起）、`backend/app/etl/reader.py`（仅新增辅助，不改通用语义）。
- 设计：
  - 布局探测：取业务名列「需求明细.数据ID(不可修改)」位置 D；**D<44 → 91 列新布局**
    （头段 [0..6]∪[44..90]，明细段 [7..43]），**D≥44 → 90 列旧布局**（头段 [0..52]，明细段 [53..89]）
    （事实档案 §1.1）。位置数学自检：91 列=头 54＋明细 37；90 列=头 53＋明细 37
    （差集恰为 91 列独有的头列「是否可以接受通用号」）。两式不成立 → 整批拒绝 `layout_unknown`。
  - 取列规则：**按业务名定位**为主（兼容 `(必填)`/`(不可修改)` 后缀变体——现有
    `canonicalize_columns`/`_strip_opt`，`backend/app/etl/mapping.py:252/232`），段位仅做**防御校验**：
    头字段命中位置必须落在头段、明细字段必须落在明细段，越段 → 整批拒绝 `segment_mismatch`。
  - 【v1.1 修正】v1.1 §3.1 称「新 91 列布局需求数量/备注/图片/附件 头、明细重名，需放行重复业务名」——
    **前提不成立**：明细列业务名均带「需求明细.」前缀，两份真实导出经程序化核验**无精确重复列名**
    （事实档案 §1.1；复审核验结论「部分成立」，事实档案 §4 行 1）。防御策略收窄为：
    若未来出现跨段重名，按段解析天然区分；**同段重名才触发** `require_clean_columns`
    fail-closed（`backend/app/etl/reader.py:826-830`，pipeline 调用点 `etl/pipeline.py:296-299`）。
  - 名称含斜杠的单列「整机/备件」「图片/附件」不得被当作两列（映射表键原样含斜杠）。
- 测试断言（合成 fixture，禁真实数据——`import-field-contract.md` §9.2）：
  - 91 列合成文件：探测=新布局；54 头列/37 明细列全部命中段位；
  - 90 列合成文件：探测=旧布局；`accept_generic_flag` 为 NULL；
  - 头字段被挪入明细段的畸形文件 → 422 `segment_mismatch`，零写入；
  - 同段人为重名 → `require_clean_columns` 整批拒绝；
  - 92 列未知布局 → `layout_unknown` 零写入。
- 验收标准：两布局 golden fixture 全绿；灰度演练中用两份真实文件复核（§7.4，仓库内不存真实文件）。

#### M1-2 保留「有单头、无明细」订单

- 改动文件：`backend/app/etl/transform.py:218-229`。
- 现状（复审已核验成立，事实档案 §4 行 2）：行缺「明细数据ID」即记 `missing_raw_id` 错误行跳过
  （`transform.py:220-223`），单头丢失。
- 改为：`raw_order_id` 有、`raw_line_id` 空 → 单头入库、明细 0 行，计入 `headless_orders` 计数与清单；
  `raw_order_id` 空仍为错误行；`order_no` 空仍整行跳过（`missing_order_no`，防 ffill 串号，
  `transform.py:224-229` 语义保留）。
- 测试断言：合成文件含 3 个无明细单头 → orders=+3、lines=+0、`headless_orders=3`、errors 不含这 3 行；
  重传幂等（再传行数不变）；有明细订单行为不回归（现有 `backend/tests/test_maintenance_hardening.py` 全绿）。
- 验收标准：真实 2026-08-16 文件在灰度库导入后 单头 6,913 全数入库（v1.1 §1 基线，实测复核）。

#### M1-3 映射扩展（34/28 列）

- 改动文件：`backend/app/etl/mapping.py`（`MAINTENANCE_HEAD` L87-102、`MAINTENANCE_LINE` L103-111、
  `FFILL_COLS` L201）。
- 设计：按 §3 数据模型的 34+28 清单逐列加映射（业务名→列名）；头级新列进入 `FFILL_COLS[MAINTENANCE]`
  （沿用现有合并单元格下填语义）；「是否可以接受通用号」仅 91 列布局存在，90 列文件该列缺失 → NULL。
- 测试断言：映射表键数=现有 14+34（头）/ 7+28（明细）；91 列 fixture 每个新列至少一行有值断言落库；
  90 列 fixture `accept_generic_flag` 全 NULL；`是/否` 之外的旗标值 → NULL＋批次 issue 计数。
- 验收标准：§3 清单与 `mapping.py` 逐列一致（评审对照）。

#### M1-4 DDL 迁移（本计划迁移 1/2）

- 新文件：`backend/alembic/versions/<rev>_wbdd_display_columns.py`，
  `down_revision = "f1b3d5e7a9c2"`（当前分支唯一 head，`uv run alembic heads` 已核实）。
- 内容：§3 的 34+28 列 `add_column`，**全部 nullable、无 default、无 backfill、无新索引**；
  downgrade 仅 drop 本迁移新增列（展示列可由重导重建，无数据保护顾虑；生产回滚仍不走 downgrade，铁律 7）。
- 测试断言：新建 `backend/tests/test_maintenance_wbdd_display_columns_migration.py`（模仿
  `test_maintenance_collection_reminders_migration.py:228` 家族）：
  - 新修订是 `f1b3d5e7a9c2` 的**加法独子**且全链单 head（ScriptDirectory.get_heads）；
  - 62 列名/类型/nullable=True 逐列 inspect 断言；无索引/约束新增；
  - downgrade→upgrade 往返幂等；CI `alembic check` 零漂移（`.github/workflows/ci.yml:30-36`）。
- 验收标准：迁移测试绿；`uv run --extra dev alembic upgrade head && alembic check` 通过。

#### M1-5 loader upsert 白名单扩展

- 改动文件：`backend/app/etl/loader.py`（`_MAINT_ORDER_UPD`/`_MAINT_LINE_UPD` L346-351）。
- 设计：34/28 新列全部加入白名单（快照重传可刷新展示列）；**成本回填列继续排除**
  （L344-345 注释既有约定：cost、双税、reference_* 只由 `maintenance_cost.recompute` 写）。
  upsert 键不变：`raw_order_id`/`raw_line_id`（`loader.py:509-511/528-530`，冲突处理 `_upsert_facts` L273-325）。
- 测试断言：重传修改了「收货人」的同 raw_order_id 文件 → receiver 更新；预置 unit_cost 后重传 →
  成本列原值不动（扩展现有幂等测试）；skip 模式（on_conflict_do_nothing）行为不回归。
- 验收标准：升级后重传真实文件（灰度），行数不变、新列覆盖、成本零改写（对账报告断言）。

#### M1-6 专用上传端点与动作键

- 新文件：`backend/app/api/maintenance_wbdd_import.py`、`backend/app/services/maintenance_wbdd_import.py`；
  改动：`backend/app/permissions.py`（ACTION_KEYS/LABELS/PERMISSION_META/UI_GROUPS/ACTION_PAGE_DEPENDENCIES）、
  `backend/app/main.py`（挂 router）。
- 契约见 §4.1。要点：
  - 权限：`require_page("page_maintenance")` ＋ `require_action("action_maintenance_wbdd_import")`（新键，
    `ACTION_PAGE_DEPENDENCIES → page_maintenance`；**不挂数据组依赖**——WBDD 导出无任何价格列，
    `mapping.py:85-86` 注释可证；对比：`action_maintenance_doc_import` 因三单含成本列而要求
    `data_purchase_cost`，`backend/app/api/maintenance_doc_import.py:22`）；
  - 文件门：入口先跑 `mapping.detect_file_type`（`mapping.py:272-303`，WBDD 判据=需求单号∧(需求类型∨维保起始日期)），
    非 MAINTENANCE → 422 `not_wbdd_file` **零写入**——这实现了「WBDD-only 账号传采购/销售/库存/报销文件均被拒」
    （复审 P0-6 的等价物：该账号根本没有 `page_import`，连 `/api/import/upload` 都是 403）；
  - 幂等：`Idempotency-Key` 头 8–128 字符（沿用 doc-imports 约定）＋file_hash 记录；
  - flag：`maintenance_boss_dashboard_enabled=false` 时本 router 全部 404（模式同
    `backend/app/maintenance_beta.py:10` 的 `require_maintenance_beta`，新写 `require_maintenance_boss`）。
- 权限键回填迁移（本计划迁移 2/2）：`<rev2>_maintenance_boss_permissions.py`
  （`down_revision=<rev1>`）——`page_maintenance_boss` ＋ `action_maintenance_wbdd_import`
  写入 5 个内置模板与全部存量账号，**一律 false**（含 admin 模板快照；运行时 admin 走
  `require_action` 内置 bypass，无需模板值），模仿
  `backend/tests/test_maintenance_beta_access_migration.py` 断言存量账号 fail-closed。
- 测试断言：新建 `backend/tests/test_maintenance_wbdd_import_api.py`：
  - 矩阵：无 token 401；有 page_maintenance 无 action 403；有 action 200；admin 200；
  - 传销售/采购/库存/报销合成文件 → 422 且 f_* 表零行；
  - 同 Idempotency-Key 重放 → 200 返回原批次报告，不重复写；
  - flag off → 404，flag on → 200（try/finally 改 `get_settings()`，模式同 `test_maintenance_beta_gate.py:84`）。
- 验收标准：矩阵测试全绿；`/api/import/upload` 行为零改动（现有 import 测试不回归）。

#### M1-7 快照差异报告

- 改动文件：`backend/app/services/maintenance_wbdd_import.py`。
- 设计（按 F1 默认）：apply 成功后对比「本批文件内 order_no 集合」vs「库内现存active WBDD 单」：
  上批有本批无 → `snapshot_diff.missing_orders` 计数＋样例单号（≤50 个）写入批次 `report_json`；
  不删除、不打标（删除只走安全删除通道 `backend/app/api/maintenance_demands.py` 既有 intent 流程）。
- 测试断言：第二批比第一批少 2 单 → `missing_orders=2` 且这 2 单仍可查询；tombstone 单不计入 missing
  （已删除属预期缺失，`beta_active_demand_condition` 语义，`services/maintenance_demands.py:117-125`）。
- 验收标准：对账报告含 diff 段；看板「需关注」可引用该计数（M3-6，待 M0-A）。

#### M1-8 成本回填接线

- 改动文件：`backend/app/services/maintenance_wbdd_import.py`（apply 尾部）。
- 设计：复用 `maintenance_cost.recompute(db)`（`maintenance_cost.py:341`；advisory lock 忙 →
  `MaintenanceCostRecomputeBusy` → HTTP 409＋`Retry-After: 5`，同 `api/maintenance.py:156-162`）；
  语义同 `/api/import/upload` 的 `_post_import_refresh`（`api/imports.py:112-114`：recompute 失败
  记日志回滚重算但**不影响已完成导入**）。**引擎零改动**（铁律 2）。
- 现行引擎口径备案（描述现状，非重设计）：A0 direct（采购单挂 WBDD 号加权价）→ A1 window
  （±`MAINT_PRICE_WINDOW_DAYS=7` 天最近采购）→ A2 month_avg → B1 pool_purchase/pool_sales
  （互通池 3 个月）→ B2 purchase_history/sales_history（本 PN 3 个月）→ C manual → D none
  （`maintenance_cost.py:5-16` docstring；`config.py:234-240`）。即「采购价优先→无采购用销售价→时间就近」
  的现行实现；需求文档的「五层」为历史称谓，以代码为准。
- 测试断言：导入后 `cost_source` 分布可复现基线口径（合成数据小样）；recompute 忙 → 409。
- 验收标准：灰度库导入真实文件后，`recompute` 统计（lines_in_scope、各 source 计数）与生产基线
  （事实档案 §3 分布）**精确一致**（确定性计算，不允许 ±2%，见 §8）。

#### M1-9 对账断言套件

- 新文件：`backend/tests/test_maintenance_wbdd_reconciliation.py`。
- 断言（冻结合成快照下，全部**精确相等**）：
  1. 文件单头数=orders 入库数＋错误清单数；文件明细行数=lines＋错误行数；headless 单独对平；
  2. Σ需求数量、Σ需采数量（明细级）文件 vs DB 精确相等；
  3. 幂等：重传后全部计数不变；
  4. 成本：recompute 前后 `_MAINT_LINE_UPD` 白名单外零字节变化（成本列审计）。
- 验收标准：套件绿；灰度用真实文件重复以上四条（人工核对表归档进发布记录）。

---

### 2.3 M2 稳定项目归属（第 1–2 周，4–5 人日）

复用决策（复审核验，事实档案 §4 行 5）：**复用 `maintenance_source_order_assignment`，不新建第二套关系。**
模型现状（已核实）：`backend/app/models/maintenance_source_assignment.py:12-80`——
`source_order_id` 外键**只指向** `f_maintenance_order.raw_order_id`（WBDD 天然是唯一可归属源，无需
source_type 判别列）；部分唯一索引 `ux_maintenance_source_assignment_active_order`（WHERE is_active）
保证每单至多一条活跃归属；ADR-0002 三原则（名称只是线索/人工确认/关系不进成本计算）继续有效。

#### M2-1 归属候选生成（只出候选，不自动写）

- 改动文件：`backend/app/services/maintenance_source_assignments.py`（`list_source_orders` L57）、
  `backend/app/api/maintenance_source_assignments.py`（GET 目录 L62 加参数）。
- 设计：GET `/maintenance/project-assignments/orders?include_candidates=true` 时每行附
  `candidates`（≤5，见 §4.2 契约）：
  - 匹配键=`f_maintenance_order.project_std`（ETL 已剥「预交付-」前缀：`transform.py:199`
    `_PROJECT_PREFIX`，半/全角/长横杠容差）；
  - 一级：`lower(project_std) == lower(project_code)` 精确命中（已有唯一索引
    `ux_maintenance_project_code_ci`）→ `match_type=exact, score=1.0`；
  - 二级：pg_trgm 相似度（已有 `ix_maintenance_project_code_trgm`/`display_name_trgm` GIN 索引）
    `similarity ≥ 0.6` → `match_type=trgm`，按 score 降序；
  - **多候选/低分不自动**；确认走既有 `POST /assign`（乐观版本＋reason＋审计，
    `api/maintenance_source_assignments.py:113`），解除走 `POST /unassign`。
- 测试断言：新建 `backend/tests/test_maintenance_source_assignment_candidates.py`：
  精确命中排首位 score=1.0；「预交付-X」单命中真实项目 X；相似度 0.59 不出现；
  接口纯只读（前后 assignment 行数不变）；q 搜索与分页不回归（现有 API 测试全绿）。
- 验收标准：候选生成纯只读；确认/解除幂等与审计沿用既有测试。

#### M2-2 未归属桶

- 改动文件：无（读侧复用）。`assignment_status=unassigned` 目录（`services/maintenance_source_assignments.py:93-98`）
  即未归属桶；M3 看板以 `project_id="unassigned"` 伪桶聚合展示（§4.5），**不静默丢单**。
- 测试断言：并入 M3 恒等式测试（§2.5 M3-5：项目汇总＋未归属桶=全局母集）。

#### M2-3 预交付挂靠方案

- 推荐方案 B（默认）：**不新增任何列**。预交付单据经 M2-1 候选（project_std 已剥前缀）→ 人工确认
  归到真实合同项目；展示层「预交付」徽标=该单 `project_raw` 以「预交付」开头（正则复用
  `transform.py:199`）。项目行附 `pre_delivery_order_count` 计数。两个项目档案**不合并**（铁律 4：
  并入展示，不叫合并——单据归属指向真实项目，`project_raw` 完整保留预交付事实）。
- 备选方案 A（仅当台账为预交付建了**独立项目档案**时启用）：`maintenance_project` 加 nullable 自引用列
  `attached_to_project_id`（纯加法迁移），看板父项目行并入子项目数字＋下钻保留分组。**默认不做**，
  按 M0 附带项 **F4（台账预交付建档口径）**的结论二选一。
- 【技术债备案，随 M2 顺手统一】仓库现存 3 处「预交付」剥前缀正则实现不一致：
  `etl/transform.py:199`（横杠必需）、`services/date_loose.py:32`（横杠可选，另剥「预付/预」）、
  `services/maintenance_roundtrip.py:3099`（仅半角横杠）。M2 抽出单一共享函数并让三处引用
  （行为以 `transform.py` 版为准），防止归属与展示口径漂移。
- 测试断言：「预交付-平安银行…」单 → 候选=「平安银行…」；确认后看板计入真实项目且带徽标；
  三处正则统一后原有测试（`test_maintenance_hardening.py:255-256` 等）不回归。
- 验收标准：38 个预交付项目名（事实档案 §3）在灰度候选报告中全部给出真实项目候选或明确落入未归属桶。

#### M2-4 项目主数据维护路径（复用，零新代码）

- 改名：`PATCH /maintenance/projects/stable/{project_id}`（仅 display_name/project_manager_id 可改，
  `services/maintenance_project_catalog.py:182-184`）；
- 维保期限：项目表**无期限列**（已核实 `models/maintenance_project.py:26-94`）——期限在合同行
  `MaintenanceProjectContract.effective_from/effective_to`（`models/maintenance_project.py:122-123`），
  编辑走 `PATCH /maintenance/projects/stable/contracts/{id}`（`api/maintenance_project_operations.py:403`），
  台账导入亦回填（`services/maintenance_ledger.py:725-726`）；lifecycle=ongoing/ended/missing 由
  `_lifecycle_status` 推导（`maintenance_ledger.py:501-510`）。看板「期限缺失」显示即 missing
  （生产 415 项目当前全 missing——台账未导入生产，事实档案 §3；**M2 验收含台账首次正式导入**，
  端点已存在：`/maintenance/ledger-imports/*`，`action_maintenance_ledger_import`）。
- 验收标准：灰度完成一次台账导入后，lifecycle 非 missing 项目数 > 0 并在看板可见。

#### M2-5 候选质量报告（灰度侧，只读）

- 在灰度库对全量 415 项目跑一遍候选生成 → 输出报告（命中率/多候选率/零候选清单），**不写库**；
  报告归档发布记录。人日含在 M2 内。

---

### 2.4 M4 正确事实接线（第 2 周，M3 之前，5–6 人日）

三源的**导入与落库均已存在**（本里程碑是读侧接线＋readiness，零 schema 变更）：

| 事实 | 数据源（已存在） | 落库（已存在） | 代码坐标 |
|---|---|---|---|
| 实发 | CKD 发货单「维保供货」＋数据状态=已生效 | `maintenance_ckd_head_row`/`maintenance_ckd_line_row`（＋front_stock 账本 shipment_in） | `services/maintenance_ckd_import.py:449-506` |
| 未用件收回 | return_order 返库单，测试结果=成品 | `maintenance_doc_head_row`/`line_row`（＋front_stock return_out；坏品行跳过账本：`services/maintenance_doc_import.py:552-554`） | 同左 |
| 坏件回收 | rkd_inbound 入库单，类别∈{维保拆旧返件, 旧库退返}（业务已终确认）、测试结果∈{坏品,坏件,故障,废品} | `maintenance_rkd_return_line` 规范事实 | `services/maintenance_doc_import.py:604-701`；`config.py:246/250` |

#### M4-1 数据源健康服务（readiness/as_of）

- 新文件：`backend/app/services/maintenance_source_health.py`；契约 §4.4。
- 设计：四源（wbdd/ckd/return_order/rkd_inbound）各自独立计算：
  - `readiness`：无 applied 批次 → `not_imported`；最新 applied 批次 `issue_rows>0` 或存在
    project_id 未解析头行 → `partial`；否则 `ready`（批次表：`maintenance_ckd_import_batch`、
    `maintenance_doc_import_batch`（doc_type 判别）、WBDD 用 `sys_import_batch.file_type='maintenance'`）；
  - `as_of`：applied 批次内行级业务日期最大值（CKD=head `order_date`、doc=`head_date`、
    WBDD=`f_maintenance_order.order_date` max）——批次表无 as_of 列（已核实），行级聚合，**不加列**；
  - `batch_id`/`uploaded_at` 一并返回；`stale` 是**展示态**（读时判 as_of 距今>45 天，F2 默认），不落库。
- 测试断言：新建 `backend/tests/test_maintenance_source_health.py`：零批次 → not_imported；
  applied＋issue>0 → partial；applied 干净 → ready；as_of=行级最大业务日期；failed 批次不计入。
- 验收标准：**未导入的源在任何展示层绝不出现 0**——API 层返回 `state=not_imported` 且 `value=null`（§4.6 信封）。

#### M4-2 项目＋PN 聚合读模型（M0-D 默认粒度；可先行开发，**M0-D 书面追认前不合入**）

- 新文件：`backend/app/services/maintenance_boss_facts.py`。
- 设计（全部只读聚合，来源行级表，不依赖 front_stock 余额）：
  - 实发：`maintenance_ckd_line_row` join head（applied、维保供货、已生效），项目解析=head.wbdd_no →
    `f_maintenance_order.order_no` → 活跃 assignment → project_id（发货单**无项目名列**，WBDD 单号是
    唯一可靠路径——事实档案 §2.1）；group by (project_id, pn)；
  - 未用件收回：`maintenance_doc_line_row`（return_order、applied、test_result=成品）join head，项目解析
    沿用 `_resolve_project_id` 三级（wbdd_no→assignment / xsdd_no→合同 / project_name→project_code，
    `services/maintenance_doc_import.py:428`）；group by (project_id, pn)；
  - 坏件回收：`maintenance_rkd_return_line`（project_id 非空为前提）group by (project_id, pn)；
  - 未关联行（project 解析失败）：计入源级 `unlinked_rows` ＋「未关联清单」（§4.4），**不摊进任何项目、
    也不丢**；歧义清单：同项目同 PN 多需求单导致无法行级分配的场景仅在明细下钻标注（M0-D 聚合粒度下
    天然规避）。
- 测试断言：合成三源数据 → 项目 PN 聚合数=手工期望；project 解析三级顺序正确；unlinked 行单独计数；
  「成品」不进坏件、坏件枚举外的 test_result 不出现（导入层已 fail-closed）。
- 验收标准：单一项目抽查（灰度）：三源数字与原始 Excel 手工汇总一致。

#### M4-3 上传顺序无关（relink）

- 改动文件：`backend/app/api/maintenance_doc_import.py`（新端点）、`backend/app/services/maintenance_doc_import.py`。
- 背景：`maintenance_doc_head_row.project_id` 在 apply 时解析；若 RKD/返库先传、WBDD/归属后建，
  head 行 project_id 为 NULL（迁移 `d7f1a3c5e8b2` 只做过一次性回填）。
- 设计：`POST /api/maintenance/doc-imports/relink-projects`（权限同 doc-imports：page_maintenance＋
  `action_maintenance_doc_import`＋data_purchase_cost；admin/boss 可全量）：对 project_id IS NULL 的
  applied 头行重跑 `_resolve_project_id`，返回 `{relinked, still_unlinked}`；幂等；
  M2 的 assign/unassign 成功后**自动触发**一次同逻辑（复用 `reconcile_project_assignment_links` 挂点，
  `services/maintenance_source_assignments.py:351/455` 既有调用位）。
- 测试断言：新建 `backend/tests/test_maintenance_doc_relink_projects.py`：先传 RKD（unlinked=n）→
  传 WBDD＋确认归属 → relink 后 unlinked 减少且事实聚合出现在对应项目；重复 relink 幂等；权限矩阵。
- 验收标准：「先传 RKD 后传 WBDD 也能关联」演练通过（灰度按此顺序实测）。

#### M4-4 自报列与事实并排（无判定）

- 改动文件：`backend/app/services/maintenance_boss_facts.py`（M4-2 同文件：按单返回自报四列＋
  三源事实数的读模型函数；M3-2 的 orders 端点仅透出该函数结果，M4 期内以服务层＋单测交付）。
- 设计：单据下钻（§4.5 orders 接口）每单返回自报四列（head_shipped_qty/head_returned_qty/
  head_demand_qty/head_purchase_qty，M1 新列）与三源事实数**并排原样展示**。
  【v1.1 修正】v1.1 §5-5 的「差异只提示不拦截」被移除：计算差异并渲染提示违反铁律 3 字面
  （「不计算、不标注」）——**服务端不产出任何差异/判定字段**，肉眼对比由并排布局完成；
  如业务确需差异提示，走 M0 附带项 F5 书面豁免后另行实现。
- 测试断言（`backend/tests/test_maintenance_boss_facts.py`）：响应含自报四列与事实数并排字段；
  响应 JSON 中**不存在**任何 mismatch/diff 类键（防回归断言）；自报 NULL 原样返回 null。
- 验收标准：灰度抽查一单：自报与事实两组数字与原始 Excel 逐项一致；界面并排可见、无系统判定标记。

---

### 2.5 M3 决策看板（第 3 周，最后建设，8–10 人日；**M0 拍板前不开工**）

#### M3-1 聚合服务与六种空值状态信封

- 新文件：`backend/app/services/maintenance_boss_board.py`。
- 首屏五段（复审 P0-7 顺序）：`来源健康 → 本期变化 → 需关注事项 → 全项目分页列表 → 单据/PN 证据下钻`。
- 所有可空指标统一信封（§4.6）：`restricted / not_imported / partial / ready / stale / error`——
  权限不可见 / 未导入 / 部分关联 / 就绪 / 数据过期 / 计算失败；前端逐状态渲染（§5），**不共用 null**。
- **状态列白名单断言**：聚合只允许引用 `qty`、`return_qty`、成本列、三源事实表；28 个流转状态列
  （purchased_qty/supplied_qty/consumed_qty 等）**禁止进入任何聚合表达式**（铁律 3）——以单测锁死
  （聚合服务导出 `AGGREGATE_SOURCE_COLUMNS` 常量，测试断言其与状态列集合交集为空）。
- 时间窗：字段名 `orders_ytd`/`lines_ytd`，窗口由 `from`/`to` 参数决定（默认当年 1-1 至今），
  **不写死年份**（复审「y2026_ 写死」问题的修正）。
- 项目集合口径：**全量项目（415，事实档案 §3）＋未归属桶**；YTD 有单项目（226，v1.1 §1）加
  `has_activity_in_window` 标记，不作为默认过滤。

#### M3-2 看板只读端点（共 7 个）

契约详见 §4.4/§4.5。新文件 `backend/app/api/maintenance_boss_board.py`（router 受
`require_maintenance_boss` flag 门）：

| # | 端点 | 内容 |
|---|---|---|
| 1 | GET `/api/maintenance/boss-board/health` | 四源 readiness/as_of/batch |
| 2 | GET `/api/maintenance/boss-board/summary` | orders_ytd/lines_ytd/成本五件套＋环比 |
| 3 | GET `/api/maintenance/boss-board/attention` | 需关注 ≤10 条（内容按 M0-A） |
| 4 | GET `/api/maintenance/boss-board/projects` | 全项目分页列表（服务端分页/筛选/排序） |
| 5 | POST `/api/maintenance/boss-board/projects/search` | 自由文本搜索兄弟端点（GET 带 q 返 422 的仓库约定） |
| 6 | GET `/api/maintenance/boss-board/projects/{project_id}/orders` | 单据下钻（project_id 可为 `unassigned` 伪桶） |
| 7 | GET `/api/maintenance/boss-board/orders/{source_order_id}/lines` | PN 证据行下钻 |

#### M3-3 权限与脱敏（无侧信道）

- 查看键：`page_maintenance_boss`（全范围，新键）/ `page_maintenance`（既有键，范围按 M0-B：
  若定「本人项目」则走 `resolve_visible_project_ids`（`api/maintenance_project_scope.py`，
  demands/doc-imports 已用同一解析器）；若定「全部」则两键同范围，仅入口不同）。
- 金额/成本两组独立：申请成本五件套挂 `data_purchase_cost`；未来合同/回款金额挂 `data_profit`
  （v1 不展示，E3）。脱敏走既有 `security.apply_field_visibility`（`permissions.py:14` DATA_GROUPS →
  `config.FIELD_GROUPS`；新字段名注册进 `purchase_cost` 组）。
- **无侧信道三条硬规则**（复审 P0-6）：
  1. 无 `data_purchase_cost` 时：成本类字段返回 `{"state":"restricted"}`（无 value、无 as_of、无占位数字）；
  2. `sort=known_cost`/成本筛选参数 → 422 `sort_requires_cost_permission`（不静默降级——静默降级会
     通过顺序泄露排名）；attention 队列中成本派生条目整条剔除；
  3. 响应形状恒定：restricted 与 ready 的 JSON 键集合一致（防「字段存在性」侧信道）。
- 测试断言：新建 `backend/tests/test_maintenance_boss_board_permissions.py`（模仿
  `test_page_permission_contracts.py` 的 `admin_client`/`_account` 模式）：三类账号 × 全部端点矩阵
  （§6.2 表）；无成本账号响应中不含任何数字型成本值（递归扫描 JSON）；排序参数 422；
  restricted/ready 键集合相等断言。

#### M3-4 分页/筛选/排序与性能门

- 分页遵循仓库既有约定（`page: Query(1, ge=1)`＋`page_size: Query(20, ge=1, le=200)`——仓库默认值
  在 20–50 间浮动、上限一律 200，本计划取 20；同款锚点 `api/maintenance_project_operations.py:962`
  cost-gaps 端点；响应 `{rows,total,page,page_size}`）；
  自由文本搜索走 POST `/projects/search` 兄弟端点（GET 带 q 返 422 的既有约定，
  `api/maintenance_projects.py:285-289`）。
- SQL 计数门（常开测试，模仿 `test_maintenance_roundtrip_performance.py:48` 的 `_count_sql`）：
  新建 `backend/tests/test_maintenance_boss_board_perf.py`——1,000 合成项目下 projects 列表
  `selects ≤ 12` 且与 100 项目时相等（O(1) 查询数，禁 N+1）；orders/lines 下钻 `selects ≤ 8`。
- p95 门槛（灰度实测，415 项目）：列表 < 800ms、明细下钻 < 1.5s（v1.1 §8 保留）。
  未达标预案：物化每日聚合表（新增迁移，列入风险表 R6，不在本计划默认范围）。

#### M3-5 对账恒等式（验收即对账）

- 新建 `backend/tests/test_maintenance_boss_board_reconciliation.py`（合成冻结快照）：
  1. **母集恒等式**：Σ(各项目 orders/lines/成本五件套) ＋ 未归属桶 ＝ 全局母集（同 filter 同 as_of），
     **精确相等，不允许 ±2%**；
  2. summary 的 orders_ytd/lines_ytd = 按窗口直接 count 的结果；
  3. 成本五件套内部恒等：actual+estimated=known；actual_lines+estimated_lines+missing_lines=lines_in_scope；
     coverage_pct=round((actual_lines+estimated_lines)/lines*100,1)（口径与 `maintenance_cost.py:972-974` 一致）；
  4. 灰度复核：生产快照数字对齐事实档案 §3（19,046/38,483/88.7%/来源分布逐项）。

#### M3-6 需关注事项（待 M0-A）

- 队列 ≤10 条，服务端组装；每条含 `{kind, project_id?, evidence_link, value}`；候选 kind 见 §2.1 M0-A。
  M0-A 拍板前本任务只搭框架（空队列＋kind 注册表），不预置内容。

#### M3-7 前端页面（§5 详述）

---

### 2.6 M5 灰度与生产闸门（第 3–4 周，4–6 人日）

#### M5-1 迁移链审计与发布决策【v1.1 修正】

v1.1 §7「从分支提交中只提取本计划所需迁移」**不可执行**：分支相对 main（生产 head
`c8e2a4f6b1d3`，v122 终点）新增的是**线性链 12 个修订**（已核实 down_revision 逐个成链）：

```
c8e2a4f6b1d3(生产) → e7b3d9f2c1a4 台账导入 → b1e3f7d9c2a5 前置库 → c3b5d9e1f7a2 不返还规则*
→ d1e3f5a7c2b9 CKD → e9f2d4b7a1c6 三单批次 → f1a2b3c4d5e6 AI映射*
→ a7c3e5f9b2d1 RKD事实 → b9d1e7c3f5a8 变卖* → c3e9d1b7f5a2 回款凭证*
→ d7f1a3c5e8b2 doc头项目 → e3c5a7f9d1b2 变卖成本* → f1b3d5e7a9c2 凭证去重*
→ [新] wbdd_display_columns → [新] maintenance_boss_permissions
```
（* = 冻结功能所属迁移。注意 `b1e3f7d9c2a5` 前置库为**双用途**未加星：其 schema 服务在用的
CKD/返库导入账本写入（§2.4 事实表），冻结面仅为「前置库账本页」前端页——审计表须注明，
不得因无星漏审其冻结面。）

- **工程推荐（待 M0-E 签署，本计划不单方面定案）**：整链发布。理由：线性链无法抽取中段；
  12 个修订经逐一审计以 create_table/add_column/create_index 为主（本计划撰写时已初审）；
  每个修订均有迁移测试；冻结功能的表**空置无害**。铁律 7 的「纯加法 nullable」在此严格适用于
  本计划**新增的 2 个迁移**；既有 12 修订中的非加法点是既成事实，须经 M0-E 由铁律 owner 改判/豁免。
- **不推荐**：重写链只留所需修订——需重造迁移与测试、破坏 alembic 历史、灰度失去与分支
  一致的验证对象，风险远大于收益（亦为 M0-E 选项②，供业务否决）。
- M5-1 任务=补一份逐修订审计表（`docs/releases/v1.23-migration-audit.md`），对以下**非纯加法点**
  逐个写明影响与旧应用兼容结论（本计划初审已定位）：
  `e7b3d9f2c1a4`（3×drop_constraint——核验为约束替换）、`c3b5d9e1f7a2`（alter_column＋drop_constraint）、
  各修订中的 `op.execute()` 回填语句逐条审阅（确认无破坏性 UPDATE/DELETE）。
- 冻结功能三重封存核对表（发布前逐项打勾，**从运行容器读回核验**，同 M5-3 flag 读回法）：
  1. 导航隐藏：`frontend/src/nav.tsx` 无冻结页入口；betaFeature 组随 flag 关闭；
  2. 服务端 flag **全量四个**：`maintenance_beta_enabled=false`（Beta 工作台整组 404，
     `maintenance_beta.py:10`）、`maintenance_collection_plan_apply_enabled=false`、
     `replenishment_beta_enabled=false`（补库购物车独立总闸，`config.py:81`＋
     `api/replenishment.py:41`，**不在** maintenance_beta 覆盖范围内）、
     `llm_mapping_external_enabled=false`（AI 兜底外呼闸，`config.py:58`）；
  3. 动作键与挂载（按冻结功能逐项）：变卖/凭证/回款计划等专属 `action_*` 存量账号全 false
     （既有回填迁移已保证，spot-check）；**例外：AI 兜底列映射无专属动作键**——
     `api/maintenance_ai_fallback.py:26-32` 复用 `action_maintenance_doc_import`/
     `action_maintenance_ledger_import`（本计划上传台与台账导入必须授予的键），其封存依赖
     Beta 挂载闸（`main.py` maintenance_beta_dependencies）＋外呼闸，核对表按此两闸打勾，
     不得以「动作键全 false」误判封存。

#### M5-2 feature flag 与门依赖

- 改动文件：`backend/app/config.py`（`maintenance_boss_dashboard_enabled: bool = False`）、
  新 `require_maintenance_boss` 依赖（模式抄 `maintenance_beta.py:10-31`：flag off → 404
  「页面不存在」）挂在 wbdd-imports 与 boss-board 两个 router；`backend/app/beta_access.py`
  `beta_feature_availability` 增加 `"maintenance_boss"` 键（flag ∧ 实名 ∧ (page_maintenance_boss ∨
  page_maintenance)）→ 前端经 `GET /api/auth/beta-features` 感知（`auth.py:264-281` 既有通道）。
- 测试断言：flag off：新端点全 404、稳定端点（`/api/maintenance/projects` 等）200 不回归；
  flag on：矩阵按 §6.2。

#### M5-3 发布工件（模仿 v122 全套）

- 新文件：`.deploy/v123_maintenance_boss_{build.sh,rehearse.sh,release.sh,manifest.py,static_test.py}`＋
  `backend/tests/test_v123_maintenance_boss_release_control.py`（模式=
  `test_v122_collection_reminders_release_control.py`：subprocess 驱动脚本＋stub docker，断言
  manifest 绑定、阶段状态机 preflight→freeze→backup→restore_checked→compose_installed→migrated→
  deployed→observe→commit、篡改拒绝、脏工作区拒绝）。
- 迁移阶段规则（抄 v122 release.sh:2414 语义）：alembic 从 `c8e2a4f6b1d3` 升至新 head 时
  **强制 `MAINTENANCE_BOSS_DASHBOARD_ENABLED=false`**；flag 翻转是独立后置阶段，翻转后从运行容器
  **读回环境变量核验**（不信任 staged .env），失败走 emergency trap 复位（v122 release.sh:2448-2480 模式）。
- 精确 SHA：manifest 绑定 TARGET_SHA/PARENT_PROD_SHA（=`4f8b6881` 系）＋三工件 sha256；
  独立复审对准 TARGET_SHA。

#### M5-4 演练（rehearse）

1. 冷备＋从生产快照建灰度库（`.deploy/backup.sh`、`restore_drill.sh` 既有工具）；
2. 迁移演练：`c8e2a4f6b1d3 → 新 head` 真实执行＋前后 `alembic_version` 核验（v122 rehearse 模式）；
3. **旧应用兼容新 schema**：生产镜像连升级后灰度库跑冒烟（新增列全 nullable＋新表旧应用不引用 →
   预期零影响；这是「回滚=关 flag 不降 schema」的前提证明）；
4. 真实文件对账：91 列/90 列两份需求单＋CKD/返库/RKD 样例全量导入，跑 §2.2 M1-9 四条＋
   §2.5 M3-5 恒等式＋基线比对（19,046 母集、88.7% 覆盖、来源分布——事实档案 §3 逐项精确）；
5. M2-5 候选质量报告；M4-3 顺序无关演练（先 RKD 后 WBDD）。

#### M5-5 灰度与真实账号矩阵

- 三类真实账号（老板全范围/项目经理/无权限）＋admin 走 §6.2 HTTP 矩阵逐格实测；
- p95 实测（415 项目）：列表 <800ms、明细 <1.5s；
- 观察期（≥1 个上传周期）：一次真实周度上传全流程（上传→对账报告→看板刷新→as_of 前移）。

#### M5-6 回滚

- **回滚=关 `maintenance_boss_dashboard_enabled`**（新端点 404、导航入口消失、稳定功能不受影响）；
  schema 保留（不做 downgrade——铁律 7）；灾难恢复走已演练备份（M5-4-1）。

---

## 3. 数据模型（表/列清单）

### 3.1 f_maintenance_order 新增 34 列（迁移 1/2，全 nullable）

现有 18 列不动（`backend/app/models/maintenance.py:71-98`）。类型记号沿用仓库 `_types.py`：
`Qty`=数量 Numeric、`Money` 不涉及（WBDD 无价格列）。

| # | 列名 | 类型 | 源字段（业务名） | 说明 |
|---|---|---|---|---|
| 1 | head_demand_qty | Qty | 需求数量（头段） | 头级自报汇总，仅展示（M4-4 无判定并排） |
| 2 | head_purchase_qty | Qty | 需采数量（头段尾部） | 同上 |
| 3 | head_shipped_qty | Qty | 已发货数量 | 自报；与 CKD 事实无判定并排（M4-4） |
| 4 | head_returned_qty | Qty | 已返货数量 | 自报；与返库/RKD 事实无判定并排 |
| 5 | maintainer_raw | String(64) | 维保负责人 | 展示/排查 |
| 6 | work_order_no | String(64) | 维保工单 | 展示 |
| 7 | created_by_raw | String(64) | 制单人员 | 展示 |
| 8 | purchaser_raw | String(64) | 采购员 | 展示 |
| 9 | purchaser2_raw | String(64) | 采购人员 | 展示（与采购员并存的第二列） |
| 10 | project_manager_raw | String(64) | 项目经理 | 展示 |
| 11 | project_manager_staff_raw | String(64) | 项目经理人员 | 展示 |
| 12 | co_salesperson_raw | String(64) | 协同销售人员 | 展示 |
| 13 | partner_raw | String(64) | 合作伙伴人 | 展示 |
| 14 | sales_dept_raw | String(64) | 销售部门 | 展示 |
| 15 | warehouse_keeper_raw | String(64) | 仓管员 | 展示 |
| 16 | storage_center | String(64) | 仓储中心 | 展示 |
| 17 | warehouse_raw | String(64) | 仓库 | 展示（与既有 warehouse=出库仓库(必填) 并存） |
| 18 | change_warehouse_flag | Boolean | 是否变仓库 | 是/否→bool；其他值→NULL＋批次 issue |
| 19 | change_warehouse | String(64) | 变更仓库 | 展示 |
| 20 | change_warehouse_handler | String(64) | 变更仓承办人(必填) | 展示 |
| 21 | warehouse_handler | String(64) | 仓库承办人(必填) | 展示 |
| 22 | supply_deadline | Date | 供货期限 | `date_loose` 归一化；失败→NULL＋issue |
| 23 | delivery_address_option | String(128) | 选择收货地址 | 展示 |
| 24 | receiver | String(64) | 收货人 | 敏感（contract §2.3）：入 `customer_info` 数据组脱敏 |
| 25 | receiver_phone | String(32) | 收货人电话 | 同上 |
| 26 | receiver_address | Text | 收货地址 | 同上 |
| 27 | express_no | String(128) | 快递单号 | 展示 |
| 28 | express_no2 | String(128) | 快递单号# | 展示 |
| 29 | image_urls | Text | 图片 | 原样 URL 文本；不下载不代理 |
| 30 | attachments | Text | 附件 | 同上 |
| 31 | whole_machine_check | String(16) | 整机需采备件校验 | 原样 |
| 32 | accept_generic_flag | Boolean | 是否可以接受通用号 | **91 列布局独有**；90 列文件→NULL |
| 33 | created_at_raw | String(32) | 创建时间(必填) | 原样文本（不做时区解析，展示用） |
| 34 | modified_at_raw | String(32) | 修改时间(必填) | 同上 |

**显式不落库（头级 6 列）**【v1.1 静默排除，本版明示】：`项目经理#`（氚云内部人员 ID 重复列）、
`备注`（头级；明细级 line_note 已收，头级备注留待业务提出再加——同为纯加法可随时补）、
`数据标题`、`创建人(必填)`、`拥有者(必填)`、`所属部门(必填)`（氚云系统元数据列）。
核对式：头 54 列 = 既有映射 14 ＋ 新增 34 ＋ 排除 6 ✓（事实档案 §1.2/§1.4）。

### 3.2 f_maintenance_line 新增 28 列（同一迁移，全 nullable）

现有 34 列不动（含成本回填列，`models/maintenance.py:101-159`）。**下表 1–14 即「流转状态列」，
只展示、禁止进入任何计算**（铁律 3；M3-1 白名单单测锁死）。

| # | 列名 | 类型 | 源字段（业务名，前缀「需求明细.」略） | 说明 |
|---|---|---|---|---|
| 1 | purchase_qty | Qty | 需采数量(必填) | 状态列 |
| 2 | change_warehouse_purchase_qty | Qty | 变更仓需采数量(必填) | 状态列 |
| 3 | purchased_qty | Qty | 已采数量 | 状态列（历史版非空 9,540 行——事实档案 §1.3） |
| 4 | pending_purchase_qty | Qty | 待采数量 | 状态列 |
| 5 | direct_ship_qty | Qty | 直采直发数 | 状态列 |
| 6 | warehouse_need_qty | Qty | 库房需发数 | 状态列 |
| 7 | warehouse_shipped_qty | Qty | 库房发货数 | 状态列 |
| 8 | supplied_qty | Qty | 已供数量 | 状态列 |
| 9 | pending_supply_qty | Qty | 待供数量 | 状态列 |
| 10 | returned_qty | Qty | 已返数量 | 状态列 |
| 11 | pending_return_qty | Qty | 待返数量 | 状态列 |
| 12 | consumed_qty | Qty | 领用数量 | 状态列（29,140 行仅 18 行非空——事实档案 §1.3，不可信直接证据） |
| 13 | demand_pending_return_qty | Qty | 需求待返数 | 状态列 |
| 14 | return_old_part | String(16) | 退返旧件 | 状态类文本 |
| 15 | whole_or_part | String(8) | 整机/备件 | **名称含斜杠的单列** |
| 16 | whole_machine_purchase_part | Text | 整机需采备件 | 展示 |
| 17 | whole_machine_part_purchased | String(16) | 整机备件已采 | 展示 |
| 18 | purchase_note | Text | 需采备件说明 | 展示 |
| 19 | line_note | Text | 备注 | 展示 |
| 20 | line_image_urls | Text | 图片/附件 | **名称含斜杠的单列**，原样 URL |
| 21 | warehouse_stock_raw | Text | 各仓库存 | 原样多仓文本 |
| 22 | adjust_warehouse_flag | Boolean | 个别调整发货仓 | 是/否→bool；其他→NULL＋issue |
| 23 | adjust_warehouse | String(64) | 调整仓库 | 展示 |
| 24 | adjust_storage_center | String(64) | 调整仓储中心 | 展示 |
| 25 | adjust_keeper | String(64) | 调整库管员 | 展示 |
| 26 | ship_warehouse | String(64) | 发货仓库 | 展示 |
| 27 | ship_warehouse_object_id | String(64) | 发货仓ObjectID | 氚云对象 ID，排查用 |
| 28 | ship_stock | Qty | 发货库存 | 数值化失败→NULL＋issue |

**显式不落库（明细 2 列）**：`数据标题`、`产品名称#`（系统/重复列）。
核对式：明细 37 列 = 既有映射 7 ＋ 新增 28 ＋ 排除 2 ✓（事实档案 §1.2/§1.4；
「整机/备件」「图片/附件」为斜杠单列）。

### 3.3 其余表：零变更

- `maintenance_source_order_assignment` / `maintenance_project` / 三源批次与行表 / front_stock：
  **不改**（M2/M4 全部读侧复用）；
- 迁移 2/2 为**纯数据迁移**（权限键回填，见 §2.2 M1-6），无 DDL；
- 预交付方案 A 的 `attached_to_project_id` 仅在 M0 确认需要时追加（纯加法，默认不做）。

---

## 4. API 契约（端点/参数/响应/权限）

通用：所有新端点挂 `require_maintenance_boss`（flag off→404）；错误体沿用仓库惯例
`{"code": ..., "message": ...}`；分页响应 `{rows, total, page, page_size}`。

### 4.1 WBDD 专用上传（M1）

```
POST /api/maintenance/wbdd-imports
  权限: page_maintenance + action_maintenance_wbdd_import（admin bypass 保留）
  请求: multipart file=.xlsx；Header Idempotency-Key: 8–128 字符
  行为: detect_file_type 必须=maintenance（否则 422 not_wbdd_file 零写入）；
        布局探测（91/90）→ 按段解析 → upsert 快照 → 快照差异 → recompute
  200: {
    "batch_id", "file_hash", "layout": "91"|"90",
    "head_rows", "line_rows", "headless_orders", 
    "errors": {"missing_raw_id": n, "missing_order_no": n, "other": n},
    "upsert": {"orders_inserted","orders_updated","lines_inserted","lines_updated"},
    "totals": {"sum_qty","sum_purchase_qty"},          // 与文件精确对平（M1-9）
    "snapshot_diff": {"prev_batch_id","missing_orders": n,"sample_order_nos": [≤50]},
    "recompute": {"lines_in_scope","by_source": {...}}  // 与基线精确比对
  }
  409: {"code":"recompute_busy"} (Retry-After: 5) ；同 Idempotency-Key 重放→200 返原报告
  422: not_wbdd_file | layout_unknown | segment_mismatch（均零写入）
GET /api/maintenance/wbdd-imports/latest
  权限: page_maintenance（读健康信息，无 action）
  200: {"readiness","as_of","batch_id","uploaded_at","head_rows","line_rows"}
```

### 4.2 归属候选（M2，扩展既有端点）

```
GET /api/maintenance/project-assignments/orders?assignment_status=unassigned&include_candidates=true
  权限: page_maintenance（既有）；纯只读
  行 rows[] 增量字段: "candidates": [
    {"project_id","project_code","display_name","match_type":"exact"|"trgm","score": 0–1}
  ]  // ≤5，exact 恒排首；score<0.6 不返回
确认/解除: 既有 POST /assign、/unassign 原样（action_maintenance_project_manage + data_profit）
```

### 4.3 成本五件套（M0-C 键名固定，文案后定）

所有出现「已知申请估算成本（含税）」处统一结构（口径：actual 源={direct,window,month_avg,manual}，
estimated 源={pool_purchase,pool_sales,purchase_history,sales_history,trace_avg,sales_ref}，
missing={none,NULL}——`services/maintenance_cost_quality.py:13-22`；含税值取 `cost_amount_inc_tax`）：

```
"known_apply_cost_inc_tax": {
  "actual_amount",        // 实际价源合计（含税）
  "estimated_amount",     // 参照价源合计（含税）
  "known_amount",         // = actual + estimated（“已知下限”）
  "missing_lines",        // 缺价行数
  "coverage_pct",         // (actual_lines+estimated_lines)/lines*100，1 位小数
  "quality": "actual_only"|"contains_estimate"|"incomplete"   // summarize_aggregate 既有枚举
}
```
缺价语义：`quality=incomplete` 时前端必须显示「不完整/已知下限」，**绝不按 0 计**（铁律 5 精神＋
需求定义 §3.2）。

### 4.4 看板首屏（M3/M4）

```
GET /api/maintenance/boss-board/health
  权限: page_maintenance_boss 或 page_maintenance（范围见 §6.2）
  200: {"sources": {
    "wbdd":        {"readiness":"ready|partial|not_imported","as_of","batch_id","uploaded_at","unlinked_rows":0},
    "ckd":         {...,"unlinked_rows": n},      // wbdd_no 未命中归属的行数
    "return_order":{...,"unlinked_rows": n},
    "rkd_inbound": {...,"unlinked_rows": n}
  }}
GET /api/maintenance/boss-board/summary?from&to      // 默认当年 1-1 至今
  200: {"window":{"from","to"},
        "orders_ytd": Stat, "lines_ytd": Stat,
        "known_apply_cost_inc_tax": Stat<§4.3 结构>,
        "prev_window": {...同构，环比基期...}}
GET /api/maintenance/boss-board/attention?limit=10
  200: {"items":[{"kind","project_id?","value","evidence_link"}]}   // kind 集合待 M0-A
POST /api/maintenance/doc-imports/relink-projects        // M4-3
  权限: page_maintenance + action_maintenance_doc_import + data_purchase_cost
  200: {"relinked": n, "still_unlinked": n}
```

### 4.5 项目列表与下钻（M3）

```
GET /api/maintenance/boss-board/projects
  参数: page=1, page_size=20(≤200), lifecycle=ongoing|ended|missing|all,
        sort=attention|orders|name|known_cost(需成本权限), from/to, has_activity(bool)
  200 rows[]: {
    "project_id"|"unassigned", "project_code", "display_name",
    "lifecycle": "ongoing|ended|missing",          // missing 显示「期限缺失」
    "has_activity_in_window": bool, "pre_delivery_order_count": n,
    "orders_ytd": Stat, "lines_ytd": Stat,
    "known_apply_cost_inc_tax": Stat,              // data_purchase_cost 门
    "shipped_qty": Stat,                           // CKD 事实（项目+PN 聚合的项目卷积）
    "returned_good_qty": Stat,                     // return_order 成品
    "returned_bad_qty": Stat                       // rkd_inbound 返件类
  }
  未归属桶: 恒为独立一行（project_id="unassigned"），计数=未归属 WBDD 单聚合
POST /api/maintenance/boss-board/projects/search   // body {q(≤128), page, page_size}
GET /api/maintenance/boss-board/projects/{project_id}/orders?page&page_size
  200 rows[]: {"source_order_id","order_no","order_date","data_status",   // 原样展示
    "project_raw","is_pre_delivery": bool,
    "line_count","known_apply_cost_inc_tax": Stat,
    "self_report": {"head_demand_qty","head_purchase_qty","head_shipped_qty","head_returned_qty"},
    "facts": {"shipped_qty": Stat,"returned_good_qty": Stat,"returned_bad_qty": Stat}}
    // 自报与事实仅并排返回；服务端不产出任何 mismatch/差异字段（铁律 3，M4-4/F5）
GET /api/maintenance/boss-board/orders/{source_order_id}/lines?page&page_size
  200 rows[]（PN 证据行，21 列，状态列原样）:
    pn_std|pn_raw, description, qty(需求), purchase_qty(需采), purchased_qty(已采),
    pending_purchase_qty(待采), direct_ship_qty, warehouse_need_qty, warehouse_shipped_qty,
    supplied_qty, pending_supply_qty, return_qty(退货), returned_qty(已返),
    pending_return_qty(待返), consumed_qty(领用), demand_pending_return_qty(需求待返),
    known_apply_cost_inc_tax(行级，data_purchase_cost 门), cost_source+confidence(取价来源),
    pool: {"in_pool": bool, "pool_name", "pool_status": "active"|"archived"},  // archived 黄色警示
    serial_numbers(发货SN)
```

### 4.6 六种空值状态信封（全部 Stat 字段统一）

```
Stat = {"state": "ready"} & {"value": number, "as_of": date?}      // 就绪
     | {"state": "partial", "value": number, "unlinked": n}         // 部分关联（数字为下限）
     | {"state": "stale", "value": number, "as_of": date}           // as_of 距今>45 天（F2）
     | {"state": "not_imported"}                                    // 该源无 applied 批次；无 value
     | {"state": "restricted"}                                      // 权限不可见；无 value/as_of
     | {"state": "error"}                                           // 上游计算失败；无 value
```
硬规则：`not_imported`/`restricted`/`error` **一律无 value 键值（null），前端绝不渲染 0**（铁律 5）。

---

## 5. 前端（合并版：设计系统 + boss 五页 + 既有页面重构）

### 5.0 设计系统（所有新旧维保页面统一遵守，全文见附录 A）

**信息四层**（每条信息上页面前先落层；一页有且只有一条决断层）：

| 层 | 回答什么 | 放什么 | 呈现规格 |
|---|---|---|---|
| 决断层 | 现在要我做什么 | 一句结论 + 一个主操作按钮 | 全页唯一色块底；逾期转红 |
| 指标层 | 这个项目健康吗 | 最多 3 个数＋细水位条 | 数值 19px 加粗，标签 11.5px 灰 |
| 事实层 | 数据长什么样 | 合同/回款/领用/报销/单据记录 | 常规表格；跨行事实压成一行 |
| 依据层 | 凭什么这么算 | 协议版本、data_version、稳定 ID、来源单号、哈希、归属证据 | 折叠「查看数据依据」，默认收起永不删除 |

三条硬规则：依据层永远收起永远保留；两个并列主操作=没有主操作；`数据更新至 YYYY-MM-DD` 全页只出现一次（右上角）。

**状态语义六类**（只有逾期/待办允许暖色；暖色=你要动手）：

| 类别 | 含义 | 呈现 |
|---|---|---|
| 逾期 overdue | 已过截止，你要动手 | 红点+加粗 |
| 待办 actionable | 未到期，你要动手 | 橙点+加粗 |
| 正常 settled | 已完成 | 绿点，常规字重 |
| 不适用 n/a | 规则决定不参与 | 灰点半透明，无外框 |
| 受限 restricted | 权限所致 | 🔒+灰字，无外框 |
| 阻塞 blocked | 缺前置口径，不是你能修的 | 靛紫点+「管理员处理」后缀 |

与后端六态信封（§4.6 Stat）的关系：**两个正交轴**——Stat 信封回答「这个数字能不能显示」
（由 `StatCell` 唯一渲染），状态语义回答「这条业务要不要你动手」（用于点/标签/决断条）。
映射：`restricted`→受限；`not_imported`→阻塞（「尚未导入」，admin/boss 侧附「去上传」动词，
其余角色无动词）；`error`→阻塞；`stale`→依据层黄色小注，不占用状态位。

**归属唯一**：每个业务事实只出现在一个阶段（附录 A §2.3 十八事实归属表是详情页重排唯一判据）。
**标签预算**：项目卡最多 3 个状态位（1 生命周期 + ≤2 overdue/actionable）；超出降级为
事实行/依据层/`+N` 清单。

### 5.1 boss 看板五页（新建，服务于 M3 后端七端点）

导航：新组 `grp-maintenance-boss`「维保展示板」，`betaFeature: "maintenance_boss"`
（flag off 整组隐藏＋后端 404 双保险；`frontend/src/api.ts:6-9`、`nav.tsx:34` 枚举各加一值）。

| 路由 | 页面文件（新建） | perm | 内容 |
|---|---|---|---|
| /maintenance/boss | `pages/maintenance/boss/BossOverviewPage.tsx` | anyPerm [page_maintenance_boss, page_maintenance] | 首屏五段：SourceHealthBar → PeriodDeltaCards → AttentionList → 重点项目卡（≤12，按 attention 排序）→「查看全部」 |
| /maintenance/boss/projects | `pages/maintenance/boss/BossProjectListPage.tsx` | 同上 | 全项目分页表（服务端分页/筛选/排序；未归属桶置顶第二行） |
| /maintenance/boss/projects/:projectId | `pages/maintenance/boss/BossProjectDrillPage.tsx` | 同上 | 单据列表→PN 证据行下钻（两级分页） |
| /maintenance/boss/uploads | `pages/maintenance/boss/BossUploadConsolePage.tsx` | visibleWhen: wbdd/doc 上传键 | 上传台：WBDD（新端点）+CKD/返库/RKD（既有端点）；每源显示最近批次与对账报告 |
| /maintenance/boss/master | `pages/maintenance/boss/BossProjectMasterPage.tsx` | visibleWhen: canManageProject | 改名/期限/归属确认三块（调 §2.3 既有 API；不复用 Beta 版 MasterPage） |

组件（`frontend/src/components/maintenance/boss/` 全部新建）：`StatCell`（六态渲染唯一入口，
**任何状态不渲染 0**）、`SourceHealthBar`、`PeriodDeltaCards`、`AttentionList`、
`KnownCostCell`（成本五件套）、`BossProjectTable`（ResizableTable+URL 分页）、
`OrderEvidenceTable`/`LineEvidenceTable`（下钻两级；archived 池黄色警示、active 池「通用」标）、
`SelfReportColumns`（自报列与事实列纯并排，无任何差异高亮——铁律 3）。

**boss 项目卡（复用附录 A §4 版式）**：决断层=最紧急待办；指标三槽=回款率/已知申请估算成本/
待办件数（Q1 决议；无成本权限时成本槽显示受限🔒，不占位为 0）；事实行=合同份数·合同总额·
维保至·实发/收回件数；状态位≤2；依据层默认收起。

### 5.2 既有维保页面重构（附录 A 三页，服务项目经理，PR3 文案已另有文档）

- **「我的待办」页**（新建 `MaintenanceWorkDashboardPage.tsx`，默认落地页）：
  分组固定 逾期→本周到期→资料待补→正常跟进；决断层=最紧急一条（截止日最早，Q3）；
  **每一行必须有且只有一个动词按钮**（按钮/条件/权限见附录 A §3.3）；
  `rule_key`/`close_basis`/`task_id` 等内部标识只进依据层。
- **项目总览页**（改造 `MaintenanceProjectsPage.tsx`/`MaintenanceProjectCard.tsx`）：
  项目卡按附录 A §4 瘦身（20 标签→3 状态位＋事实行＋依据层，去向表见附录 A §4.2）；
  筛选器「任务类型」9 选项下拉**替换**为按紧急度（有逾期/有待办/无待办）——内部枚举名
  （`completeness`/`collection`/`cost_ratio`）禁止出现在用户界面。
- **项目详情页**（改造 `MaintenanceProjectWorkspacePage.tsx`，五阶段脊柱=同页锚点滚动，
  不改 URL）：阶段固定 01 合同与回款 / 02 采购与备件 / 03 领用与返还 / 04 成本与费用 /
  05 验收与结项；每阶段头=序号+名称+一句业务话；阶段内按 指标→事实→状态→依据 落层；
  成本状态列四值视觉分离（已核算/缺成本/销售估算待确认(阻塞)/未纳入核算(不适用)/
  无权限(受限)）；页头 `extra` 里的标签与裸链接按 D5 移入对应阶段（附录 A §5.3）。
- **导航**（`nav.tsx`）：「我的待办」直接带数字角标；admin 多「维保数据维护」组，
  「待接入」页是说明页（可点、说明进度与联系人），不是灰按钮；冻结清单页面零入口。

### 5.3 前端测试

`StatCell.test.tsx`（六态×不渲染 0）、`BossOverviewPage.test.tsx`（无成本权限不出现金额字段；
restricted 渲染；无侧信道）、`maintenanceNavigation.test.tsx` 扩展（flag off 无入口；
角标数字；冻结页零入口）、`MaintenanceProjectCard.test.tsx` 更新（状态位≤3 断言）、
`MaintenanceWorkDashboardPage.test.tsx`（每行恰一个动词按钮；四档分组顺序）、
`MaintenanceProjectWorkspacePage.test.tsx` 更新（五阶段锚点；依据层默认收起）。

## 6. 测试与 CI

### 6.1 测试文件清单（新建，全合成数据）

| 文件 | 覆盖 |
|---|---|
| backend/tests/test_maintenance_wbdd_import_layouts.py | M1-1/2/3：双布局 golden、段校验、headless、斜杠列、后缀变体、90 列 accept_generic NULL |
| backend/tests/test_maintenance_wbdd_display_columns_migration.py | M1-4：62 列 DDL 精确断言＋加法独子＋往返 |
| backend/tests/test_maintenance_boss_permissions_migration.py | 键回填 false（模仿 test_maintenance_beta_access_migration.py） |
| backend/tests/test_maintenance_wbdd_import_api.py | M1-6：矩阵/错类型零写入/幂等/flag |
| backend/tests/test_maintenance_wbdd_reconciliation.py | M1-9：四条精确对平 |
| backend/tests/test_maintenance_source_assignment_candidates.py | M2-1/3：exact/trgm/预交付/只读 |
| backend/tests/test_maintenance_source_health.py | M4-1：readiness 状态机/as_of |
| backend/tests/test_maintenance_doc_relink_projects.py | M4-3：顺序无关/幂等/矩阵 |
| backend/tests/test_maintenance_boss_facts.py | M4-2/M4-4：项目+PN 聚合/三级项目解析/unlinked 计数/自报四列并排（无 mismatch 键防回归） |
| backend/tests/test_maintenance_boss_board_api.py | M3-2：七端点契约/信封/unassigned 桶 |
| backend/tests/test_maintenance_boss_board_permissions.py | M3-3：三类账号矩阵/无侧信道（键集合相等＋递归无数字）/排序 422 |
| backend/tests/test_maintenance_boss_board_reconciliation.py | M3-5：母集恒等式/五件套内恒等；M3-1 状态列白名单锁（`AGGREGATE_SOURCE_COLUMNS` 与 28 状态列交集为空） |
| backend/tests/test_maintenance_boss_board_perf.py | M3-4：SQL 计数门（O(1) 查询数） |
| backend/tests/test_v123_maintenance_boss_release_control.py | M5-3：发布链状态机 |
| frontend `__tests__`：StatCell / BossOverviewPage / 导航 / 项目卡 / 待办页 / 详情页五阶段 | §5.3 |

约定（既有，遵守）：pytest 单进程（xdist 拒绝，`tests/run_isolation.py:181-191`）、本地 Postgres
`127.0.0.1:5433` 独立库（`conftest.py:24`）、`migrated` fixture 走 `alembic upgrade head`
（`conftest.py:216-225`，无 create_all）；账号构造模仿 `test_page_permission_contracts.py:26-53`
（admin_client/_account）；**测试只用合成数据，不提交真实业务行**（contract §9.2）。

### 6.2 HTTP 权限矩阵（M3-3/M5-5 验收基准）

账号定义：老板=page_maintenance_boss＋page_maintenance＋data_purchase_cost（＋data_profit）；
项目经理=page_maintenance（范围按 M0-B；成本组有/无两种子案）；无权限=两页键皆无；未登录=无 token。

| 端点 | 老板 | 经理(有成本) | 经理(无成本) | 无权限 | 未登录 |
|---|---|---|---|---|---|
| GET boss-board/health | 200 全源 | 200（范围内） | 200 | 403 | 401 |
| GET boss-board/summary | 200 全量 | 200（范围聚合） | 200 但成本字段 restricted | 403 | 401 |
| GET boss-board/attention | 200 | 200（范围内条目） | 200 剔除成本派生条目 | 403 | 401 |
| GET boss-board/projects | 200 全项目+桶 | 200 范围项目（桶仅 boss/admin 可见——未归属单无归属即无「本人」范围） | 200 成本列 restricted | 403 | 401 |
| GET …/projects?sort=known_cost | 200 | 200 | **422** | 403 | 401 |
| GET …/{id}/orders、…/lines | 200 | 200（范围内 id；越权 404） | 200 成本/来源列 restricted | 403 | 401 |
| POST wbdd-imports | 200（admin/有 action） | 有 action→200；无→403 | 同左 | 403 | 401 |
| POST /api/import/upload（对照） | 无 page_import→403 | 403 | 403 | 403 | 401 |
| flag=false 时以上全部 | 404 | 404 | 404 | 404 | 404/401 |

无侧信道补充断言：经理(无成本) 的响应中——①无任何成本数值；②restricted 字段无 as_of；
③列表默认排序与老板案不同源（attention 排序含成本时对其降级为 name 排序并在响应标注
`"sort_applied":"name"`，显式而非静默）。

### 6.3 CI

现状即够：单一 `.github/workflows/ci.yml`（self-hosted cloudlay-ts）——backend job
`alembic upgrade head`＋`alembic check` 零漂移＋`uv run --extra dev pytest -q`（75 分钟额度）；
frontend job `npm ci && npm run build`（tsc＋vite）＋`vitest run`＋`audit:prod`。
新增测试自动进入全量跑；若 v123 release-control 测试拖慢 PR，可按 ci.yml:37-47 先例做 PR 号
范围 carve-out（发布 PR 只跑该文件）。**发布门=精确 SHA 上完整 CI 绿**（M5-3／§8-9）。

---

## 7. 发布/回滚

### 7.1 迁移链（见 §2.6 M5-1）

生产 `c8e2a4f6b1d3` → 分支既有 12 修订（整链发布为工程推荐，**以 M0-E 书面签署为前提**；
逐修订审计表为闸门工件）→ 本计划新增 2 修订（列迁移＋权限键回填，均纯加法/纯数据）→ 新 head。
冻结功能三重封存核对表（含四个服务端 flag 读回）随发布 checklist。

### 7.2 flag 与灰度

`maintenance_boss_dashboard_enabled` 默认 false；迁移阶段强制 false；灰度翻转带容器内读回核验＋
emergency trap（v122 模式）；观察期覆盖一个真实上传周期。

### 7.3 回滚

关 flag（即时、无 schema 动作、旧功能零影响）；不做 downgrade；灾备走已演练备份恢复。

---

## 8. 最低通过标准（全部满足才可发布）

1. **精确对平**（不允许 ±2%）：冻结快照下 文件↔解析↔DB 的单头/明细/Σ数量三级相等（M1-9）；
   recompute 来源分布与生产基线逐项相等（事实档案 §3：34,138/38,483=88.7% 及 9 项分布）；
2. **母集恒等式**：项目汇总＋未归属桶＝全局有效 WBDD 母集（19,046 历史全量/窗口子集均成立）；
3. WBDD-only 账号：传 WBDD 成功；传采购/销售/库存/报销文件 422/403 **零写入**；
   `/api/import/upload` 对其 403；
4. 三类账号 HTTP 矩阵（§6.2）逐格通过（真实账号灰度实测）；
5. 无成本权限：金额、来源、覆盖率、排序、排名全不可见且无侧信道（键集合/递归扫描/排序 422 三重断言）；
6. 415 项目：列表 p95 < 800ms、明细 p95 < 1.5s（灰度实测）＋SQL 计数门（O(1) 查询数）常开；
7. 四源各自显示 as_of/readiness；未导入显示 not_imported，**任何位置不得伪装成 0**；
8. flag=false 时新端点全 404、存量功能回归测试全绿；回滚演练（关 flag）通过；
9. CI：完整 pytest＋alembic check＋tsc/vite＋vitest 在精确发布 SHA 上全绿；
   v123 release-control 测试绿；迁移审计表与备份恢复演练记录归档。

---

## 9. 风险表

| # | 风险 | 概率 | 影响 | 缓解 | 兜底 |
|---|---|---|---|---|---|
| R1 | 氚云出现第三种导出布局（≠90/91 列） | 中 | 导入整批拒绝 | 布局位置数学自检＋`layout_unknown` fail-closed（M1-1）；探测逻辑按业务名不按列序 | 新布局=新增段常量＋golden fixture，纯代码补丁不动 schema |
| R2 | 生产 88.7% 覆盖率随新数据漂移，对账「精确相等」误报 | 中 | 灰度对账失败 | 对账基线取**冻结快照**（同一 as_of），非活动库比对 | 基线快照重采并归档新数字（须双人复核） |
| R3 | RKD/返库项目关联缺口大（宽版按单号仅 79%——事实档案 §2.2） | 高 | 事实进未关联清单，项目数字偏低 | 返库单有项目名列三级解析兜底；relink 端点＋归属确认后自动重解析（M4-3）；partial 态显式展示「数字为下限」 | 未关联清单人工治理工作流（既有 doc 头行 issues 通道） |
| R4 | 未归属桶过大（候选命中率低） | 中 | 老板看板未归属占比刺眼 | M2-5 灰度候选质量报告前置；trgm 阈值可调；预交付剥前缀提升命中 | 批量确认工具已有（assign 100/批）；桶顶置曝光倒逼治理 |
| R5 | 台账迟迟不导入生产 → 415 项目 lifecycle 全 missing、期限缺失 | 中 | 看板「期限缺失」满屏 | M2-4 把台账首次导入列为验收项；缺失显示为明确状态而非空白 | 看板不阻塞：missing 是六态外的 lifecycle 标签，正常渲染 |
| R6 | 415 项目聚合 p95 超标 | 低-中 | 未达 §8-6 | SQL 计数门早期锁 O(1)；聚合下推 SQL；灰度实测早于 M5 尾期 | 预案：每日物化聚合表（新增一个纯加法迁移，工期+2 人日） |
| R7 | 无成本权限侧信道回归（新字段泄漏） | 低 | 违反 §8-5 | 键集合相等＋递归数字扫描测试常开；新增字段必须过 FIELD_GROUPS 注册评审 | 发布前矩阵实测；泄漏=阻断发布 |
| R8 | 整链发布携带的冻结功能被误触发 | 低 | 冻结清单失守 | 三重封存核对表（导航/四 flag 容器读回/动作键与挂载逐功能核对，含 AI 兜底无专属键的例外条目——§2.6 M5-1） | 观察期监控 404/403 日志中的冻结路由访问 |
| R9 | M0 迟迟不拍板 | 中 | M3 顺延；M4-2 无法合入；M5 无法发布 | M1/M2 不依赖 A–E；M4-2 按 M0-D 默认粒度先行开发（合入前追认）；F1–F5 有默认可推进 | 里程碑顺序本身即缓冲（M3 最后建设）；M0-E 可与 M5 准备并行推进签署 |
| R10 | 双人并行 28–37 人日估算偏差 | 中 | 交付顺延 | M1-9/M3-5 对账自动化压缩联调；复用率高（M2/M4 零 schema） | 砍序：M3-6 attention（待 M0-A）与 M3-7 上传台可后置一周 |

---

## 附：v1.1 → v1.2 修正登记（复核用）

| # | v1.1 原文 | v1.2 修正 | 依据 |
|---|---|---|---|
| 1 | §3.1 需放行「重复业务名」（需求数量/备注/图片/附件重名） | 真实导出无精确重名（明细带「需求明细.」前缀）；改为按段解析＋同段重名才 fail-closed | 事实档案 §1.1、§4 行 1 |
| 2 | §3.1 引 `etl/reader.py:769` | 实际坐标：记录在 `_inspect_frame` L788，拒绝在 `require_clean_columns` L826-830，调用点 pipeline L296-299 | 代码核实 |
| 3 | §7 「只提取本计划所需迁移」 | 线性链 12 修订不可抽取；改「整链发布（工程推荐）＋逐修订审计＋M0-E 铁律 owner 书面签署」——与铁律 7 的张力显式上交，不由计划单方面改判 | §2.6 M5-1，down_revision 逐个核实 |
| 4 | 端点写作 `/api/imports/upload` | 实际为 `/api/import/upload`（prefix="/import"） | `api/imports.py:30` |
| 5 | §9 合计 29-40 人日 | 分项相加=28-37 | 算术 |
| 6 | 34/28 列仅有名单 | 补齐类型/源字段/说明＋8 个排除列显式化＋两条核对式 | §3；事实档案 §1.2/§1.4 |
| 7 | 「五层取价瀑布」 | 现行引擎层次以 `maintenance_cost.py:5-16` 为准备案（direct/window/month_avg/pool/history/manual/none）；口径不变，只接线 | 代码核实 |
| 8 | 项目期限维护「走现有 project 维护端点」 | 精确化：期限在合同行 effective_from/to，项目表无期限列 | `models/maintenance_project.py` 核实 |
| 9 | §5-5 自报列与事实「差异只提示不拦截」 | 移除服务端差异计算与提示（违反铁律 3 字面「不计算、不标注」）；改纯并排展示，业务确需提示走 F5 书面豁免 | 铁律 3 原文 |

---

## 附录 A：维保前端信息架构设计（上游原文，454 行）

# 维保前端信息架构设计

> 阶段：Plan（未进入实现）
> 基线：`origin/main@4f8b688`
> 上游：`docs/superpowers/plans/PR1-collapsible-nav.md`、`PR3-ux-refactor.md`、`docs/维保管理字段业务化改造方案.html`
> 本文回答：**展示哪些信息、怎么展示、有哪些按钮**。不含代码。

---

## 0. 本文补什么

已提交的两份文档解决的是**文案**：`项目经理→维保负责人`、`PN→备件型号`、`dry-run→预检不保存`。
这些改名都对，但改完之后信息仍然没有逻辑——因为下面六个问题都不是文案问题。

本文只补三件已有文档没有定义的东西：

1. **信息分层**：同一个页面上，哪些信息该大声说、哪些该小声说、哪些该收起来
2. **状态语义**：五种完全不同的「没有数据」，怎么让用户一眼分清哪个要自己动手
3. **归属唯一**：每个业务事实只能出现在一个地方

---

## 1. 诊断：六个结构问题

| 编号 | 问题 | 证据 |
|---|---|---|
| **D1** | **同一事实被拆散**。回款出现在 3 处（顶部进度条 / 项目跟踪卡的「下一回款计划」/ 第 6 张回款明细表），领用出现在 4 处（成本进度条 / 领用工作流面板 / 领用全量明细表 / 卡头「缺 N 行成本」）。用户要拼 4 个位置才能回答「领用到底怎么样」 | `MaintenanceProjectWorkspacePage.tsx` 8 张平铺 Card |
| **D2** | **权重全等**。KPI、工作流、原始表、协议版本全部是同款 `Card`：同白底、同阴影、同 15px 标题。视觉上没有任何东西说「先看这个」 | `index.css` → `.ant-card` 全局统一 |
| **D3** | **标签洪水**。一张项目卡最多同时渲染 20 个 `Tag`：返还率 7 + 追踪 3 + `missing_data_labels` N + 提醒 Badge + 成本 1 + 每份合同 1–3。标签本该用于「例外」，这里被用成了「陈述」 | `MaintenanceProjectCard.tsx:20–41, 152–193` |
| **D4** | **缺数据不分种类**。`missing`（你要补）、`restricted`（无权限）、`not_counted`（规则不纳入）、`basis_unknown`（口径未定）、`completeness_unknown`（系统判不了）业务含义完全不同，界面上全是灰或橙标签 | `ProjectFinancialProgress.tsx:78–90` |
| **D5** | **页头塞三类东西**。副标题串了 `编号 · 项目经理 · 数据截止`，右上角混了一个标签加两个裸链接，其中一个直接指向管理员技术页 | `MaintenanceProjectWorkspacePage.tsx:273–289` |
| **D6** | **没有下一步**。「系统提醒」渲染成一列 `Alert`，只有标题和描述，没有任何按钮。用户看到「成本待补」之后要自己回想去哪个菜单 | `MaintenanceProjectWorkspacePage.tsx:295–312` |

---

## 2. 设计规则

### 2.1 信息四层

每条信息上页面前必须先落层。一个页面**有且只有一条决断层**。

| 层 | 回答什么 | 放什么 | 呈现规格 |
|---|---|---|---|
| **决断层** | 现在要我做什么 | 一句结论 + **一个**主操作按钮 | 全页唯一允许用色块底的层；逾期转红 |
| **指标层** | 这个项目健康吗 | 最多 3 个数，带细水位条，等宽数字 | 数值 19px 加粗，标签 11.5px 灰 |
| **事实层** | 数据长什么样 | 合同、回款记录、领用记录、报销记录 | 常规表格；跨行事实压成一行文字 |
| **依据层** | 凭什么这么算 | 协议版本、`data_version`、稳定 ID、来源单号、哈希、归属证据 | 折叠区「查看数据依据」，**默认收起，永不删除** |

三条硬规则：

- 依据层永远收起，永远保留——审计证据在，但不占日常注意力
- 两个并列的「主操作」等于没有主操作
- `数据更新至 YYYY-MM-DD` 全页只出现一次，放右上角

### 2.2 状态语义五类（修 D3 + D4）

现在「不计入合同总额」（中性规则）和「缺成本」（要你干活）用同一族灰橙标签，这是「没逻辑」最直接的来源。

新规则：**只有前两类允许用暖色。暖色 = 你要动手。**

| 类别 | 含义 | 呈现 | 现在被误用成什么 |
|---|---|---|---|
| **逾期** `overdue` | 已过截止，你要动手 | 红点 + 加粗 | 红 Tag，与「负责人账号失效」同级混排 |
| **待办** `actionable` | 未到期，你要动手 | 橙点 + 加粗 | 橙 Tag，与「状态未映射」不可区分 |
| **正常** `settled` | 已完成，无需处理 | 绿点，常规字重 | 绿 Tag，占位与待办同宽，稀释警示 |
| **不适用** `n/a` | 规则决定不参与，永远不会变 | 灰点半透明，**无外框** | 灰 Tag，看起来像「缺了什么」 |
| **受限** `restricted` | 权限所致，与你的工作无关 | 🔒 + 灰字，**无外框** | 「金额不可见」，用户以为是数据缺失去找管理员补数 |
| **阻塞** `blocked` | 缺前置口径，**不是你能修的** | 靛紫点 + 「管理员处理」后缀 | 橙色警告，维保负责人反复尝试却无从下手 |

> 靛紫是新增色，因为现有 `--mb-*` 令牌里没有一个中性冷色能承担「这事跟你无关但确实卡住了」。
> 复用 danger/warning 会把它误归进「你要动手」。

### 2.3 归属唯一（修 D1）

**每个业务事实只能出现在一个阶段里。** 这是详情页重排的唯一判据。

| 业务事实 | 现在出现在 | 新归属 | 呈现层 |
|---|---|---|---|
| 回款进度 % | 顶部进度卡 | 01 合同与回款 | 指标层 |
| 下一回款节点 | 项目跟踪与验收卡 | 01（最紧急时升决断层） | 指标层 |
| 回款明细表 | 第 6 张卡 | 01 | 事实层 |
| 合同清单 | 第 4 张卡 | 01 | 事实层 |
| 合同税口径未确认 | 合同卡内橙字 | 01 | 状态位（阻塞） |
| 采购订单链 | 尚无 | 02 采购与备件 | 事实层 |
| 领用工作流面板 | 第 5 张卡 | 03 领用与返还 | 事实层 |
| 领用明细表 | 第 7 张卡 | 03 | 事实层 |
| 返还率 7 个标签 | 项目卡 | 03 | 事实行 + 1 状态位 |
| 成本进度 % | 顶部进度卡 | 04 成本与费用 | 指标层 |
| 缺成本行数 | 卡头标签 + 进度注脚 | 04 | 状态位（待办） |
| 销售回退估算说明 | 进度条注脚 | 04 | 依据层 |
| 报销明细表 | 第 8 张卡 | 04 | 事实层 |
| 验收面板 | 第 3 张卡 | 05 验收与结项 | 决断层 + 事实行 |
| 维保期限 | 第 3 张卡 | 05 | 事实行 |
| 工作簿四表下载 | 第 9 张卡 | 05 | 事实层 |
| 协议版本 / `data_version` | 第 9 张卡头 | 各阶段 | 依据层 |
| 人工归属历史单数 | 页头右上角 | 01 | 依据层 |

### 2.4 标签预算（修 D3）

**项目卡最多 3 个状态位**：1 个生命周期 + 最多 2 个 `overdue`/`actionable`。

超出的一律降级：

- 是数字 → 压进事实行（`应返 12 · 已确认 9`）
- 是审计说明 → 收进依据层
- 还有更多 → 显示 `+N`，点开是完整清单

---

## 3. 页面一：我的待办

新页面，维保负责人的默认落地页。复用 `listMaintenanceProjectOperations`，不新建后端接口。

### 3.1 结构图

```
┌──────────────────────────────────────────────────────────────────┐
│ 我的待办                                   数据更新至 2026-08-01 │
│ 张伟 · 负责 6 个维保项目                                         │
├──────────────────────────────────────────────────────────────────┤
│▌1 件已逾期，先处理它                             [ 去跟进回款 ]  │  ← 决断层（红底）
│▌某市政务云项目 · 第 3 期回款已逾期 12 天                         │
├──────────────────────────────────────────────────────────────────┤
│ ● 已逾期                                                    1 件 │
│ ┃ 第 3 期回款未到账，需登记或改期                   [ 跟进回款 ] │
│ ┃ 某市政务云项目 · 合同 HT-2024-1130                  逾期 12 天 │
│                                                                  │
│ ● 本周到期                                                  2 件 │
│ ┃ 2026-08 月度全量表尚未回传                    [ 更新本月进展 ] │
│ ┃ 某市政务云项目 · 回传后自动完成         8 月 31 日 · 还有 5 天 │
│                                                                  │
│ ┃ 3 件坏件待仓库确认签收                        [ 跟进坏件返还 ] │
│ ┃ 某省医保系统项目 · 已登记 9 / 应返 12   8 月 29 日 · 还有 3 天 │
│                                                                  │
│ ◆ 资料待补                                                  1 件 │
│ ┃ 11 条领用缺成本，成本只能给出下限             [ 补充缺失成本 ] │
│ ┃ 某市政务云项目 · 其中 6 条为销售估算                无固定截止 │
│                                                                  │
│ ● 正常跟进                                        3 个项目无待办 │
│ ┃ 某区教育城域网、某银行数据中心、某集团总部        [ 查看项目 ] │
│ ┃ 本月无到期事项                                                 │
└──────────────────────────────────────────────────────────────────┘

图例  ▌决断条   ┃ 分组色条（红=逾期 橙=本周 紫=资料待补 灰=正常）
      ● 状态点   ◆ 阻塞点   [ ] 按钮
```

### 3.2 展示哪些信息

分组顺序固定为 **逾期 → 本周到期 → 资料待补 → 正常跟进**（PR3 已定）。

| 位置 | 展示内容 | 数据来源 | 层 |
|---|---|---|---|
| 页头标题 | `我的待办` | 常量 | — |
| 页头副标题 | 负责人姓名 · 负责 N 个项目 | `manager_assignment` + 行数 | — |
| 页头右上 | `数据更新至 {as_of}` | `as_of` | — |
| 决断条 | 最紧急一条：事项 + 项目名 + 逾期天数 | `task_summary.primary`，按截止日排序取首条 | 决断 |
| 分组标题 | 档位名 + 件数 | `task_summary.rows` 按 `due_state` 分档 | — |
| 待办行主标题 | 事项说明（业务语言，非规则名） | `task.title` | 事实 |
| 待办行副标题 | 项目名 · 关键上下文（合同号／进度分数） | `display_name` + 场景字段 | 事实 |
| 待办行右侧 | 截止日 + 剩余/逾期天数 | `task.due_date`、`task.is_overdue` | 事实 |
| 正常跟进组 | 折叠成一行项目名列表 | 无待办的项目 | 事实 |

**不展示的**：`rule_key`、`close_basis`（`manager_workbook_upload_applied` 这类内部标识）、`task_id`、`generated_by`、`entity_id`。这些进依据层，日常不出现。

### 3.3 有哪些按钮

| 按钮 | 位置 | 出现条件 | 点击后 | 权限 |
|---|---|---|---|---|
| `去跟进回款` / 动态动词 | 决断条右侧 | 恒有（无待办时整条转绿「本周无到期事项」，无按钮） | 跳详情页对应阶段并展开 | 本人项目 |
| `跟进回款` | 待办行右侧 | `task_type` ∈ 计划回款/回款 | 详情页 → 阶段 01 | 本人项目 |
| `更新本月进展` | 待办行右侧 | `task_type` = 月度更新 | 月度项目更新页，预选该项目 | `canUseManagerWorkbook` |
| `登记现场领用` | 待办行右侧 | `task_type` = 领用相关 | 详情页 → 阶段 03 | `canManageSiteIssues` |
| `跟进坏件返还` | 待办行右侧 | 返还有未确认项 | 详情页 → 阶段 03 | `canManageBadReturns` |
| `补充缺失成本` | 待办行右侧 | `missing_cost_lines > 0` | 领用缺价补录页，预选该项目 | `canViewCost` + 补录权限 |
| `提交验收资料` | 待办行右侧 | 验收未提交且已配置 | 详情页 → 阶段 05 | 本人项目 |
| `查看项目` | 正常跟进组 | 恒有 | 项目总览页 | — |
| `全部项目 ｜ 我负责的` | 页头右侧分段控件 | **仅 admin/boss** | 切换范围，默认「我负责的」 | `role === admin/boss` |

**规则：每一行必须有且只有一个动词按钮。** 待办的定义是「可以被做完」；做不完的不叫待办，叫状态，应降级到项目卡的状态位。

---

## 4. 页面二：项目总览

### 4.1 项目卡结构图

```
┌──────────────────────────────────────────────────────────────┐
│ 某市政务云平台运维服务项目                        [ 服务中 ] │
│ WB-2024-0087 · 维保负责人 张伟                               │
├──────────────────────────────────────────────────────────────┤
│▌第 3 期回款逾期 12 天                          [ 跟进回款 ]  │  ← 决断层
│▌另有 3 件待办 · 最近截止 8 月 31 日                          │
├──────────────────────────────────────────────────────────────┤
│ 已回款          成本占合同        待办                       │  ← 指标层
│ 62%             ≥74%              4 件                       │
│ ▓▓▓▓▓▓░░░░      ▓▓▓▓▓▓▓░░░        ▓▓░░░░░░░░                 │
├──────────────────────────────────────────────────────────────┤
│ 合同 2 份 · 合同额 ¥186.0 万 · 维保至 2026-10-31             │  ← 事实层
│ 应返 12 · 已确认 9                                           │
│                                                              │
│ ● 11 条领用缺成本    ◆ 1 份合同税口径待确认                  │  ← 状态位（最多 2）
├──────────────────────────────────────────────────────────────┤
│ ▸ 查看数据依据                                               │  ← 依据层（默认收起）
├──────────────────────────────────────────────────────────────┤
│                           进入项目                           │
└──────────────────────────────────────────────────────────────┘

对比  现状：20 个标签 · 2 条进度条 · 7 行灰色注脚 · 无主操作 · 约 780px 高
      新设计：3 个状态位 · 3 个指标 · 1 个决断条 · 1 个主操作 · 约 330px 高
```

### 4.2 20 个标签去哪了

| 现状元素 | 去向 | 理由 |
|---|---|---|
| 返还率 7 个标签 | 事实行 + 1 状态位 | 数字是事实不是例外；只有「待确认 3」需要人做 |
| 追踪 3 个标签（维保期／下一回款／验收） | 事实行 + 决断条 | 最紧急的升为决断条，其余降为事实 |
| 合同卡 2 张（含 4 个标签） | 事实行 `合同 2 份` | 合同明细属详情页阶段 01，总览不需要 |
| 进度条 7 行灰色注脚 | 依据层 | 「销售回退估算不等于采购单价」是审计说明 |
| `missing_data_labels` 4 个橙标签 | 合并为 1 个状态位 | 合并成「11 条领用缺成本」一句可执行的话 |
| 「已人工归属历史维保单 7 张」 | 依据层 | 数据来源事实，与今天要做的事无关 |
| 「来源负责人原文：张伟」 | 依据层 | 仅在负责人绑定核对时需要 |
| `完成依据：manager_workbook_upload_applied` | 依据层 | 内部规则标识，业务层不出现 |

### 4.3 展示哪些信息

| 位置 | 展示内容 | 数据来源 | 层 |
|---|---|---|---|
| 卡头主标题 | 项目名称 | `display_name` | — |
| 卡头副标题 | `项目编号 · 维保负责人 姓名` | `project_code` + `manager_assignment` | — |
| 卡头右上 | 生命周期：服务中／已结项／期限待确认 | `lifecycle_status` | 状态位 1 |
| 决断条 | 最紧急待办 + 其余件数 + 最近截止 | `task_summary` | 决断 |
| 指标 1 | 已回款 % | `metrics.collection_progress_pct` | 指标 |
| 指标 2 | 成本占合同 %（不完整时前缀 `≥`） | `cost_rate_lower_bound_pct` + `cost_complete` | 指标 |
| 指标 3 | 待办件数（逾期数决定水位条颜色） | `task_summary.open_count/overdue_count` | 指标 |
| 事实行 | 合同份数 · 合同总额 · 维保至 · 应返/已确认 | `contracts`、`metrics`、`manager_tracking`、`return_rate` | 事实 |
| 状态位 | 最多 2 个 actionable/blocked | 见 §2.2 分类 | 状态 |
| 依据层 | `project_id`、`data_version`、人工归属单数、销售估算行数、来源负责人原文 | 各字段 | 依据 |

### 4.4 有哪些按钮

| 按钮 | 位置 | 出现条件 | 点击后 | 权限 |
|---|---|---|---|---|
| 动态动词（`跟进回款` 等） | 决断条右侧 | 有待办时 | 详情页对应阶段 | 本人项目 |
| `进入项目` | 卡片底部 | 恒有 | 详情页顶部 | 本人项目 |
| `查看数据依据` | 卡片底部折叠 | 恒有 | 原地展开 | — |
| `指派负责人` | 卡片底部 | **仅 admin** 且 `canManageProject` | 弹窗 | admin |
| `上传月度全量表` | — | **移除**，并入待办页 `更新本月进展` | — | — |

**页面级筛选**（页头下方一行）：

| 控件 | 选项 | 说明 |
|---|---|---|
| 搜索框 | 输入项目编号或名称搜索 | — |
| 期限分段 | 服务中 ｜ 已结项 ｜ 期限待确认 ｜ 全部 | 默认「服务中」 |
| 范围分段 | 全部项目 ｜ 我负责的 | **仅 admin/boss**，默认「我负责的」 |
| 待办筛选 | 有逾期 ｜ 有待办 ｜ 无待办 | **替换**现在的「任务类型」9 选项下拉 |

> 现在的「任务类型」下拉把 `completeness`/`collection`/`cost`/`cost_ratio` 这类内部规则名和中文任务名混在一个列表里，
> 是 D4 在筛选器上的体现。改为按**紧急度**筛选——用户找的是「哪些项目有事」，不是「哪条规则触发了」。

---

## 5. 页面三：项目详情

### 5.1 整页结构图

```
┌────────────────────────────────────────────────────────────────────┐
│ 某市政务云平台运维服务项目                   数据更新至 2026-08-01 │
│ WB-2024-0087 · 维保负责人 张伟 · [ 服务中 ]                        │
├────────────────────────────────────────────────────────────────────┤
│▌第 3 期回款逾期 12 天，需登记到账或改期              [ 跟进回款 ]  │  ← 决断层
│▌完成后回到正常跟进 · 其余 3 件待办均未到期                         │
├───────────────────┬────────────────────────────────────────────────┤
│ 01 合同与回款 [1] │ 01 ── 合同与回款                               │
│ 02 采购与备件  -  │      这个项目签了多少、收回来多少              │
│ 03 领用与返还 [1] │ ┌────────┬────────┬────────┐                   │  ← 指标层
│ 04 成本与费用 [1] │ │合同总额│已回款  │下一期  │                   │
│ 05 验收与结项 [1] │ │¥186.0万│62%     │逾期12天│                   │
│                   │ └────────┴────────┴────────┘                   │
│ [n]=未完成事项    │ ┌─ 回款记录 ───────────── 共 8 条 ─┐           │  ← 事实层
│ 红色=含逾期       │ │ 回款月份 对应合同 累计金额 状态  │           │
│ [-]=无事项        │ └──────────────────────────────────┘           │
│                   │ ┌─ 关联合同 ───────────────────────┐           │
│ 同页滚动高亮      │ │ HT-2024-1130 ¥1,860,000 ● 有效   │           │
│ 不改 URL          │ │ HT-2023-0412 金额未填写          │           │
│                   │ │   ○ 历史合同  ◆ 税口径待确认     │           │
│                   │ └──────────────────────────────────┘           │
│                   │ ▸ 查看数据依据                                 │  ← 依据层
│                   │                                                │
│                   │ 02 ── 采购与备件                               │
│                   │       这些备件为什么属于这个项目               │
│                   │ 尚未找到关联采购订单                           │
│                   │ （空阶段照常显示，不隐藏）                     │
│                   │                                                │
│                   │ 03 ── 领用与返还   04 ──…   05 ──…             │
└───────────────────┴────────────────────────────────────────────────┘

阶段脊柱是锚点不是路由：同页滚动 + 高亮，不改 URL、不额外请求。
```

与 PR3「阶段间切换是同一页内的 scroll/expand」一致。

### 5.2 每阶段展示什么

每个阶段头固定三件事：**阶段序号 + 阶段名 + 一句「这一阶段在处理什么业务」**。

#### 01 合同与回款 — 这个项目签了多少、收回来多少

| 层 | 内容 |
|---|---|
| 指标 | 合同总额（含税）· 已回款 % · 下一期状态 |
| 事实 | 回款记录表：回款月份／对应合同／回款金额（累计）／回款凭证／状态<br>关联合同：合同号／金额／有效性状态位 |
| 状态 | 逾期回款（红）· 税口径待确认（阻塞紫）· 合同金额未填写（待办橙） |
| 依据 | `project_contract_id`、`contract_amount_basis`、`status_mapping_state`、快照 version、人工归属历史单数 |

#### 02 采购与备件 — 这些备件为什么属于这个项目

| 层 | 内容 |
|---|---|
| 事实 | 采购订单号／日期／供应商／关联维保需求单／备件型号／数量／金额 |
| 状态 | 无关联时显示「尚未找到关联采购订单」，**不隐藏整个阶段** |
| 依据 | `linked_maintenance_order_no`、关联判定结果 |

> 无采购成本权限时金额列走**受限**类（🔒 无权限查看），订单号和数量照常显示。

#### 03 领用与返还 — 现场领了什么、坏件还回来没有

| 层 | 内容 |
|---|---|
| 事实行 | `领用 46 条 · 应返 12 · 已登记 9 · 仓库确认 9 · 免返 4`（原 7 个标签压成一行） |
| 事实 | 领用记录表：领用日期／领用单号／备件型号／数量／单价（未税）／成本状态 |
| 状态 | `● 3 件待仓库确认`（待办）· `◆ 2 件品类待判定 · 管理员处理`（阻塞） |
| 依据 | `line_id`、`order_no` 原文、适配器状态、协议版本 |

**成本状态列**是 D4 最集中的地方，四种值必须视觉分开：

| 值 | 现在 | 新呈现 | 类别 |
|---|---|---|---|
| `available` | 已计入成本（绿 Tag） | `● 已核算` | 正常 |
| `missing` | 待回填成本（橙 Tag） | `● 缺成本` | **待办** |
| `cost_is_estimate` | 已计入（估算）（金 Tag） | `◆ 销售估算，待确认` | **阻塞** |
| `not_counted` | 未计入成本（灰 Tag） | `○ 未纳入核算` | 不适用 |
| `restricted` | 成本不可见（灰 Tag） | `🔒 无权限查看` | 受限 |

#### 04 成本与费用 — 这个项目花了多少、还差多少凭证

| 层 | 内容 |
|---|---|
| 指标 | 已消耗成本 ÷ 合同总额（不完整时前缀 `≥`）· 领用成本 · 已报销费用 |
| 事实 | 报销记录表：报销单号／报销日期／合同／报销人／费用分类／费用说明／金额 |
| 状态 | `● 11 条领用缺成本`（待办）· 成本水位（正常／偏高／已超合同额） |
| 依据 | 销售回退估算行数与金额、税基口径、`cost_progress_label` |

> 现在挂在进度条下的 7 行灰色注脚（含「销售回退估算…不等于采购或人工确认单价」）全部移入依据层。
> 水位结论保留为一个状态位。

#### 05 验收与结项 — 交付材料齐了没有、能不能结项

| 层 | 内容 |
|---|---|
| 决断 | 验收资料尚未提交 · 截止日 · 剩余天数 · 已上传附件数 → `[ 提交验收资料 ]` |
| 事实行 | 维保期 `2024-11-01 ～ 2026-10-31` · 审批状态 |
| 事实 | 项目工作簿（四表下载） |
| 依据 | 协议版本、`data_version`、最近导出时间、四表行数与写入边界 |

### 5.3 有哪些按钮

| 按钮 | 位置 | 出现条件 | 点击后 | 权限 |
|---|---|---|---|---|
| 动态动词 | 页头决断条 | 有待办 | 滚动到对应阶段并展开 | 本人项目 |
| `跟进回款` | 阶段 01 | 有未处理回款节点 | 回款跟进弹窗（标记已处理／改期／重新打开） | 本人项目 |
| `导入回款计划` | 阶段 01 | **仅 admin** | 四步导入向导 | admin |
| `登记现场领用` | 阶段 03 | — | 领用登记表单 | `canManageSiteIssues` |
| `跟进坏件返还` | 阶段 03 | 有待确认返还 | 返还跟进面板 | `canManageBadReturns` |
| `补充缺失成本` | 阶段 04 | `missing_cost_lines > 0` | 缺价补录页，预选本项目 | `canViewCost` |
| `自动匹配最新价格` | 阶段 04 | 同上 | 重新匹配后回到本页 | 同上 |
| `提交验收资料` | 阶段 05 | 未提交且已配置 | 验收提交面板 | 本人项目 |
| `下载项目工作簿` | 阶段 05 | — | 下载四表 | `canUseManagerWorkbook` |
| `上传回填工作簿` | 阶段 05 | — | 预检 → 差异 → 确认导入 | 同上 |
| `查看数据依据` | 每阶段底部 | 恒有 | 原地展开 | — |

**从页头移除的**（现在挤在 `extra` 里，属 D5）：

- `已人工归属历史维保单 N 张` 标签 → 阶段 01 依据层
- `查看归属明细` 裸链接 → 阶段 01 依据层内的链接
- `去人工回填成本` 裸链接 → 阶段 04 的 `补充缺失成本` 按钮

---

## 6. 导航

结构照 `PR1-collapsible-nav.md` 已定方案，本文只补一处：**「我的待办」在侧栏直接带数字角标**。
用户不进页面就知道有没有事，这是 D6 最便宜的一半解法。

```
维保负责人所见                     admin 所见
─────────────────────             ─────────────────────
🧰 维保工作台          ▾           🧰 维保工作台          ▾
   我的待办          [4]              我的待办          [2]
   项目总览                           项目总览
   月度项目更新                       月度项目更新
   验收与结项                         验收与结项
                                   🗂 维保数据维护        ▾
（「维保数据维护」整组                 项目资料同步   待接入
  不渲染，不是禁用）                   异常维保单处理
                                      仓库单据核对   待接入
                                      历史单据归属
                                      历史数据迁移核对
```

**「待接入」不是灰按钮。** 菜单项照常可点，进去是一页明确说明：
「仓库数据尚未接入，样表与字段合同确认后开放」，附当前进度和联系人。
按计划文档的门禁要求，不把必然失败的导入按钮做成主操作。

---

## 7. 落地映射

本文不新增 PR，不改后端接口和数据流。补的是 PR3 里
「新建 `BusinessActionCard.tsx` / `TechnicalDetails.tsx` / `MaintenanceWorkflowNav.tsx`」这些条目缺少的信息规格。

| 本文产出 | 落到哪里 | 与已有计划的关系 |
|---|---|---|
| 信息四层 + 状态五类 | `maintenanceLanguage.ts`、`maintenanceOperations.css` | PR3 已建文案模块，本文补 `STATUS_KIND` 语义层与配套令牌 |
| 决断条 | `BusinessActionCard.tsx` | PR3 已列为新建，本文定义其结构与逾期变体 |
| 依据层折叠 | `TechnicalDetails.tsx` | PR3 已列为新建，本文定义收纳清单（§2.3 依据层行） |
| 五阶段脊柱 | `MaintenanceWorkflowNav.tsx` | PR3 已列为新建，本文补归属表（§2.3）与待办角标 |
| 待办四档分组 | `MaintenanceWorkDashboardPage.tsx` | PR3 已定排序，本文补版式与「每行必须有动词按钮」 |
| 项目卡瘦身 | `MaintenanceProjectCard.tsx`、`ProjectFinancialProgress.tsx`、`ContractPortfolio.tsx` | PR3 只写了文案改写，本文追加标签预算与去向表（§4.2） |
| 侧栏待办角标 | `nav.tsx`、`AppShell.tsx` | PR1 已定两组结构，本文只追加数字角标 |
| 待办筛选替换任务类型下拉 | `MaintenanceProjectsPage.tsx` | **PR3 未覆盖**，见 §4.4 |

---

## 8. 需要业务确认

| # | 问题 | 本文暂定 |
|---|---|---|
| Q1 | 指标层固定三个数（回款率／成本率／待办数）。维保负责人日常最看重的是不是这三个？若更关心返还率，需**替换**而非追加——位置只有三个 | 按现有 metrics 覆盖面暂定这三个 |
| Q2 | 「阻塞」类（税口径待确认、品类待判定）界面上明说「管理员处理」。是否需要一键转派给 admin，还是只做提示、线下沟通？ | 暂只做提示，不做转派 |
| Q3 | 决断条每页只有一条。同时存在「回款逾期」和「验收逾期」时，优先级按截止日还是按金额？ | 暂按截止日最早者 |
| Q4 | `not_counted`（未纳入核算）的业务规则是什么？确认它永远不需要人工干预，才能归入「不适用」而不是「待办」 | 暂按不适用处理 |

---

## 附：本文未涉及

- 后端接口、权限逻辑、数据模型：不改
- 具体配色值与像素规格：实现阶段按 `index.css` 现有 `--mb-*` 令牌落地，仅需新增一个「阻塞」色
- 移动端（390px）与平板（768px）断点：指标三联转纵向、阶段脊柱转横向滚动，其余不变


---

## 附录 B：合并决议记录（2026-08-16）

| 决议 | 结论 | 落点 |
|---|---|---|
| Q1 指标三槽 | 回款率 / 已知申请估算成本(占合同，不完整前缀≥) / 待办件数；返还率只作状态位+事实行；老板看板三槽按 §4.5（orders_ytd/lines_ytd/成本五件套） | §5.0/§5.1 项目卡 |
| Q2 阻塞转派 | 只提示 + 「复制描述」按钮；不建转派流程 | §5.0 阻塞类 |
| Q3 决断条优先级 | 截止日最早；金额随行展示不参与排序 | §5.0/§5.2 |
| Q4 not_counted | 归「不适用」；事实/依据层保留「该合同未计入总额（台账 included_in_total=false）」；需人工干预则升级待办（M0 追认 F6） | §5.2 成本状态列 |
| 六态信封 vs 状态语义 | 两轴正交：信封答「能否显示」（StatCell），语义答「要不要动手」（点/标签/决断条）；映射表见 §5.0 | §4.6+§5.0 |
| 后端里程碑 | M0→M1→M2→M4→M3→M3b→M5；合计 34–45 人日 | §1 |
| 前端设计系统 | 附录 A 三规则（四层/六类/归属唯一/标签预算）升级为全栈铁律 8 | §0 |
