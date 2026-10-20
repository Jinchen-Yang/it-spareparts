# ITSP-13 Round1 followups 残留核对与本次范围

日期：2026-10-20。分支：`fix/r1-followups-remainder`。核对基线：`1a743c1`（#426，启动时与 `origin/main` 一致）。决策：D-39、D-40、D-41；本次不新增业务口径。

## 范围与证据读法

本次按金臣收窄后的 lane B 指令，先核对已合入的 Round1 批修复，再补名义侧测试与前端标签/文案；另修 ManualSiteIssueSection CAS 测试等待异步按钮的确定性竞态。较大的 URL、排序契约、防抖与取消请求改造不在本次实现。只在本工作树修改、提交，不推送、不建 PR、不访问生产、不连接生产数据库、不改源数据。

“已完成”表示在本地基线代码、测试及已合提交中找到实现证据，不表示本次重新跑过该项全部测试，不表示已通过用户点按验收。旧 followups 和 PR 只用于定位遗留与提交；业务口径以 `docs/decisions/0017`（D-39）、`0018`（D-40）、`0019`（D-41）为准。时间、数量和生产观测均保留原观测日期。

下表判定与源码行号默认对应核对基线 `1a743c1`；本次完成结果见末节，新增测试用函数名定位。单头后端模块说明与前端 API 类型注释也补明缺列翻转，计算表达式保持原样。

来源简称：

- **F**：`~/workspaces/reviews/followups.md`（153 行）。
- **C**：`~/workspaces/reviews/client-req-20261016/followups.md`（32 行；大部分为 F 的重复记录）。
- **P**：`~/workspaces/reviews/client-req-20261016/plan-pr5-and-followups.md`，尤其第 79–126 行的 Batch-fix PR-A / PR-B。
- **H**：`~/workspaces/reviews/ybw-backlog-handoff-20261019.md`，ITSP-13 第 474–486 行、archive_missing 第 653–662 行。
- **U**：本轮用户补充指令（2026-10-20 第 4 任执行方）。

开工文档检查：仓库 `AGENTS.md`、`CONTEXT.md`、相关决策、`.claude/skills/run-it-spareparts/SKILL.md` 与用户指定的 `~/.claude/skills` 已读取；本基线不存在 `docs/agents/workflow.md`，不补造该文件，按 `AGENTS.md` 与本轮明确指令执行。

## 名义侧核对与本次补齐

| 条目 | 来源 | 判定 | 证据 | 本 PR 处理 |
| --- | --- | --- | --- | --- |
| #398 S1：未标税率时只给实际推算侧标名义 | F:90–96、124–127；C:13–18；P:84–86；H:480 | 已完成后端与单头展示；前端行级仍有缺口 | #401 `09f5661`（实现 `2f41703`、`5986b7c`、`05f7fd2`）；`purchase_analytics.py:77–104` 名义 SQL、`:107–147` 金额 SQL、`:150–205` Python 参照；D-39:30–39 | 补测试与抽屉行级标签，不重写已正确的金额公式 |
| 权威侧金额缺列时单头名义侧翻转；两列皆空不标名义 | U；F:#401；P:A1 | 已完成 | `5986b7c`；D-39:41–58；`test_nominal_side_legacy_gap_orders`；`test_order_amount_sql_matches_reference`；手算 H9/H10/H11 | 增加完整组合覆盖，锁定缺列回退 |
| 行级/分类口径保持结构性名义侧，与缺列单头可能相反 | D-39:51–56；P:A1/A8 | 后端已完成；抽屉明细金额与分类合计未标名义 | `purchase_analytics.py:855` 行级参照；`OrderDetailDrawer.tsx:107–112` 明细金额、`:158` 分类合计；单头 `:145/:148` 已按 `detail.nominal_side` 标记 | 先补失败测试，再按行级口径补标签及文案，覆盖单头与行级方向相反 |
| 13% / 6% / 0.6% / 0 / NULL × 含税 / 不含税 / NULL × 四种金额列状态 | P:86、102；U | 基线有分散测试，完整组合不足；本次补齐并通过 | 新增 `test_handcalc_complete_rate_flag_and_missing_column_matrix`（60 格），另有 `test_handcalc_zero_authoritative_amount_never_falls_back`（6 格） | 公共 API 与独立常量断言；四种列状态为双列有值、仅含税列、仅未税列、双列皆空；另锁定权威金额 0 不回退；旧测试保留 |
| #398 #4：手算金额测试不复用被测金额公式 | F:94；C:17；P:102 | 已完成基础覆盖；本次增强 | #401 新增 `test_purchase_analytics_amounts_handcalc.py:135/145/184/197/232` | 新增组合仍使用独立期望值，不以生产 helper 生成答案 |
| #399 #6 与 #403 名义占比文案 | F:103；P:85 | 汇总文案已完成；抽屉行级补齐见上 | #403 `eede6dd` / `c2798f2`，`SummaryView.tsx` 与对应 `PurchaseAnalyticsPage.test.tsx` | 保留已完成的单头/汇总语义，补抽屉标签 |
| #398 S2：NULL/0 与真实 0% 分布 | F:92；C:15；P:87 | 现行口径已裁定；真实免税/0%业务分布仍待金臣/生产只读核对 | D-39:60–62 已记录 2026-10-16 聚合：有效单 NULL 税率 0；0 税率按未标处理；不能据此证明不存在真实免税业务 | 不改税率口径、不访问生产；如需区别真实 0%，单独裁定 |

## Batch-fix PR-A 与后端遗留逐项核对

| 条目 | 来源 | 判定 | 证据 | 本 PR 处理 |
| --- | --- | --- | --- | --- |
| #394 文档 P1：admin 可见性、税率规则、“仍待确认” | F:27–30、36、43；P:125 | 已完成 | `8245609` / #394 `6aff684`；D-39:13、20、25–28 | 仅登记 |
| #394 分类筛选实付取行金额、不分摊差额 | F:29；P:125 | 已完成 | D-39:17、27–28；#394、#398 | 仅登记；11 单根因另列 |
| #395 A→B→A 被相同哈希吞掉 | F:32–36；P:101 | 已完成 | `ae77f8d`、#396 `983d767`；D-40:15 按订单+批次唯一；`test_aba_with_null_times_keeps_all_three_latest_is_a`、`test_import_aba_with_null_modified_times_latest_is_a` | 不重复实现 |
| #395 同修改时间/NULL 时按批次排序，不按 id 猜批次 | F:34–36；P:101 | 已完成 | `sales_reconciliation.py` latest 查询；`test_latest_same_time_tie_breaks_by_larger_batch_despite_larger_row_id`、`test_latest_null_modified_time_loses_to_any_time_then_falls_back_to_batch` | 仅登记 |
| #395 决策先于 schema 合并的流程项 | F:35、43–44；P:101 | 已完成 | #394 `6aff684` → #395 `1d5637c` → #396 `91affd0` 的 main 历史 | 仅登记 |
| #396 P1：明细校验失败仍收集单头对账值 | F:39、45；P:101 | 已完成 | `659bcb9` → `95d224e`；`test_transform_recon_collected_from_invalid_line_rows`、`test_transform_bad_recon_on_detail_invalid_row_still_reported` | 不改高风险 ETL |
| #396 P2：已填税列不同值也报告冲突，仅 NULL 补写 | F:40、45；P:101 | 已完成 | `95d224e`；`backfill_sales_reconciliation.py` 比较全部所属订单；回填 runbook:14–18 | 不改回填数据 |
| #396 自报疑点：missing_raw_id 行无法收集单头值 | F:46；F:139 | 现存路径；是否补救需新契约，缺稳定身份不能猜归属 | `transform.py:212–215` 在缺原始 ID 时跳过；已有 PR-2 对坏明细的收集修复不等于可恢复缺身份行 | 建议单独 PR/决策；不猜测单头归属 |
| #396 自报疑点：含税三件套坏值静默 None | F:46；F:139 | 现存路径；异常值告警语义待核对 | `transform.py:137–138` 坏值可为 None；已有 `test_transform_tax_trio_bad_value_falls_back_to_later_good_row` | 建议单独 PR/需金臣确认，不改 `loader.py`（亦涉及 #430） |
| #396 自报疑点：病态文件有含税额但无未税额/税率 | F:46；F:139 | 头字段可不完整；是否增加告警需单独确定 | `transform.py` 单头构造允许可空列；已有 `test_transform_tax_trio_collected_from_invalid_line_rows`；D-41 保持缺值为空，不允许猜金额 | 建议单独 PR/需金臣确认，不据金额反推缺失业务事实 |
| #396 会话 advisory lock + 池化/提交边界 | F:49–51 | 已完成；其余审计范围为事务锁 | `e3d9927`；回填 runbook:106–123 写明专用连接、锁丢失校验、文件级 xact lock | 不触碰生产，不重复改锁 |
| #397(1) / #398(3)：`date.today()` → `business_today` | F:70、84、93；C:7、16 | 已完成 | `20c6542`，参照测试 `4c0ea13`；`sales_analytics.py:147/260/322` | 仅登记 |
| #397(2)：无对账权限整块/历史归无数据形态 | F:71；P:90 | 已完成 | `034e505`；`sales_analytics.py:233`；`test_data_reconciliation_hidden_masks_reconciliation_fields:521` | 仅登记 |
| #397(3)：KPI amount_ex/inc_tax_total 进 FIELD_GROUPS | F:72；P:91 | 已完成 | `034e505`；`config.py:659–660`；`test_summary_kpi_amount_totals_masked_without_purchase_cost:262` | 仅登记 |
| #397(4)：customer/salesperson ILIKE 字面转义 | F:73；P:92 | 已完成 | `034e505`；`sales_analytics.py:59/100–104`；`test_customer_and_salesperson_wildcards_literal_only:275` | 仅登记 |
| #397(5)：business_type 索引 / EXPLAIN | F:74；P:93 | 索引已完成；生产性能结论待只读核对 | `16caf7b`；迁移 `c9d4f2a6b8e1` 的 `ix_so_status_btype`；`test_recon_fk_and_btype_index_migration.py` | 不新增重复索引，不访问生产 |
| #397 carry-over：reconciled_as_of 时区 | F:108；C:31；P:95 | 已完成 | `034e505`；`sales_analytics.py:185` `_utc_iso`；`test_reconciliation_timestamps_independent_of_session_timezone:322` | 仅登记 |
| #395 HALF_UP / HALF_EVEN 注释 | F:55–56；P:96 | 已完成 | `86cd2e2`；`sales_reconciliation.py:27–36` 解释 parse_money HALF_EVEN 与已量化输入的 HALF_UP 幂等性；`test_canonical_amount_idempotent_after_parse_money:332` | 不重复改注释/舍入规则 |
| #395 sales_order_id FK 索引与 ON DELETE | F:57；P:96 | 已完成 | `16caf7b`；迁移 `c9d4f2a6b8e1`，`ON DELETE SET NULL` 与索引；模型 `sales.py:125–126/165` | 不新增 migration |
| #396 S5：共享快照行构造 | F:60、65；P:98 | 已完成 | `86cd2e2`；`sales_reconciliation.py:99/132`；loader:776 与回填:414 同用 `snapshot_rows_from_orders`；`test_loader_written_rows_equal_shared_builder` | 不改高风险 ETL |
| #396 S6：skip 含税补写批量化 | F:61、65；P:99 | 批量目标已完成；loader 的 SQL 形状与原建议不同 | `86cd2e2`、`5986b7c`；回填脚本 `:597` 是分块 UPDATE FROM VALUES；loader `:730` 是按块/列 INSERT ON CONFLICT UPDATE，并非逐行；`test_tax_fills_batch_update_null_only_in_batch`、`test_tax_fills_chunked_under_bind_parameter_limit` | 不为追求 SQL 字面形式重写；需性能证据再单独 PR |
| #396 S7：删除 identity `_doomed` | F:62、65 | 已完成 | `e3d9927`；当前 loader 无 `_doomed` | 仅登记 |
| #396 S10：对账坏值为告警计数 | F:63；P:100 | 已完成 | `86cd2e2`；`transform.py:19–27` SOFT_ERROR_TYPES；`loader.py:918–923/994` | 仅登记 |
| #398 页码/ID bounds、空串关联、分类下钻类别 | F:84、95；C:7、18 | 已完成 | `20c6542`；`8b2c5d3`；API 越界 422 与详情关联测试 | 仅登记 |
| #398 S6 / #401 S6：requested sort 与 effective_sort（含 DrillTable） | F:95、107、138；C:30；P:88 | 采购 API 已完成；两页前端与销售 API 仍未统一 | 采购 API `2088586`；采购 `DrillTable.tsx:67/76` 仍读请求 sort、父页 `:255` 传请求；销售 `sales_analytics.py:301` 只回有效 sort；销售 `DrillTable.tsx:52–54` 读 `data.sort` | 建议单独 PR；涉及 API、类型、前端与回退行为，不在收窄范围实现 |
| #398 / #401 S8：全年采购 orders 性能 | F:53、139 | 已有分页/有界关联优化；现行生产性能待只读核对 | `d94e8f3`、后续 `3217d1c`；`purchase_analytics.py:690–695` 有界关联、`:825–838` 详情单点查；`test_purchase_analytics_equivalence.py`；旧报告 2.5–2.7s 不能当现行结论 | 建议单独 PR，待生产只读 profiling；本次不压测 |
| #400 S7：Excel xx:xx:59.999 分布 | F:132；P:103 | 待金臣/生产只读核对 | 本地来源只提出风险，没有现行分布与正确舍入的业务裁定 | 不猜测、不改 ETL 或时间舍入 |
| #401 S3：迁移锁/行数发布闸门 | F:136–137 | 已完成文档 | #403 `4fd2334`；`docs/releases/v1.37-round1-analytics-runbook.md:15–59` | 保留带日期的历史观测，执行时另行门控复核 |
| PR-5 带日期筛选导致审计 JSONB 丢行 | F:148–152 | 已完成 | #411 `b60b95a` / `56c8c5a`；`test_access_log_serialization.py:89/204` | 仅登记 |

## Batch-fix PR-B 与前端遗留逐项核对

| 条目 | 来源 | 判定 | 证据 | 本 PR 处理 |
| --- | --- | --- | --- | --- |
| #399(1)(2)：业务日与无效自定义区间提示 | F:97–99；C:21–22 | 已完成 | `e3ddfa9` / `8b2c5d3`，DateRangePresets 与对应测试 | 仅登记 |
| #400 重复 DateRangePresets / AmountBasisSelect 合并冲突 | F:87；C:10 | 已完成 | #400 已合入 `97849be`，共享组件保留业务日与范围提示 | 无需重做 rebase |
| #399 #3 客户选项全历史 summary 反查 | F:100；C:23；P:111 | 已完成 | `eca73f8` / #402；`PurchaseAnalyticsPage.tsx:97–108` 改用 options，后端 `purchase_analytics.py:405–411` 有界候选；页面测试 `:373` 明确不请求全历史客户金额汇总 | 不改选项契约 |
| #399 #4 / #400 S4：多值重复参数兼容旧逗号链接 | F:101、116；C:24；P:107 | 已完成 | `ead238b`；`utils/analyticsUrlState.ts:35`，`url_version=2`；共享 URL 契约测试 | 不重复重构 |
| #399 #5 / #400 S4：下钻 push、筛选 replace | F:102、116；C:25；P:108 | 已完成打开/筛选；关闭对称另列 | `ead238b`；共享 helper `analyticsUrlState.ts:92`；测试“筛选/分页/排序 replace，下钻 push” | 关闭抽屉见 #402 S2；其余保留 |
| #399 #7 / #400 S4：pageSize 入 URL | F:104、116；C:27；P:109 | 已完成 | `ead238b`；共享 helper:28；两页 `urlState.ts` 读取/写入 `page_size` | 仅登记 |
| #400 S2：采购 page clamp 1..10000 | F:114；P:110 | 已完成 | `ead238b`；`analyticsUrlState.ts:21–25`；两页共享 | 仅登记 |
| #399 S8 / #400 S1：测试时钟固定 UTC ISO | F:109、113；C:32 | 已完成 | `8ca3f86`；DateRangePresets / PurchaseAnalyticsPage / SalesAnalyticsPage 测试使用带 Z 时间 | 保留；本次相关测试跨三时区验证 |
| #400 MUST：“数据截至时间”按 Asia/Shanghai | F:112；P:112–117 | 已完成 | `8ca3f86`，后续 `39f7a0c`；sales shared 复用 `formatBusinessDateTime` | 仅登记 |
| #400 S3：“无对账数据”按响应而非仅本地权限 | F:115 | 已完成 | `8ca3f86`；销售详情响应字段检查及测试 | 仅登记 |
| #400 S5：无偏移 ISO 固定 UTC | F:130；P:113 | 已完成 | `39f7a0c`；`utils/date.ts:57–79`；`date.test.ts:81` | 不重复改解析规则 |
| #400 S6：微秒与 Safari | F:131；P:114 | 微秒测试与可移植解析已完成；无本轮 Safari 实机证据 | `39f7a0c`；数字构造时间避免 Safari 长小数解析；`date.test.ts:85` | 不宣称 Safari 已实测；本次不扩大浏览器验证 |
| GovernancePage:383 原 UTC 切片 | F:117–118；P:115 | 已完成 | `39f7a0c`；现 `GovernancePage.tsx:386`；BusinessTimestampSites 测试 | 仅登记 |
| ReplenishmentBetaPage 两处浏览器时区 | F:120；P:116 | 已完成 | `39f7a0c`；现 `:239/:1156` 使用共享 formatter | 仅登记 |
| ReturnReceiptImport / SiteReturnTab / ReturnReceiptsSection 无 timeZone | F:121；P:116 | 已完成 | `39f7a0c`；现 `:47` / `:302` / `:593/:601/:765/:985` | 不触碰并行维保变更文件 |
| DataQualityIssuesPanel 私有 formatter 合并 | F:122；P:117 | 已完成 | `39f7a0c`；现 `:222/:433/:442/:444/:452` 使用共享 formatter；src 剩余 `toLocaleString` 为数字格式 | 仅登记 |
| CollectionTab teardown window is not defined | F:79–80；C:2–3；P:119 | 已有稳定化修复 | `e769e3b`；CollectionTab cleanup、`cleanupAntd` 与 `await act` | 不凭历史 flaky 再扩大改动；相关新失败才重新定位 |
| ReturnReceiptBatchMaintenance teardown | F:52 | 已有稳定化修复 | #402 / `ba43b10` 修改该测试并清理全局通知 | 同上 |
| PartSearchPage / ReturnReceiptBatchEntry 负载超时 | F:81、133；C:4；P:120 | 已有稳定化修复 | `e769e3b`；对应测试清理、`act` 与等待条件 | 不跑全量/负载测试；保留观察 |
| MaintenanceProjectPanelPage flaky | F:81、133、145；C:4；P:120 | 已有稳定化修复；当前文件与 #427/#432 冲突 | `e769e3b`；只读 `gh pr view 427/432 --json files` 均列该测试 | 按 U 跳过该文件，后续单独验证 |
| ManualSiteIssueSection CAS / 提交更正 flaky | F:145 | 确定测试等待目标不充分，本次最小稳定化 | 测试 `:381` 等 preview API 调用、`:382` 同步查“提交更正”；组件 `:559` await 响应、`:578` setPreview、`:832` 才换按钮；`4be5bb9` 仅改无动画 App render | 先用受控 Promise 复现“已调用 API 但按钮未提交”，再改为 `await findByRole`；只改测试，不改组件，不删旧断言 |
| 可选 CI 全局 pin TZ | P:121 | 可选，未要求实施 | 已有固定 UTC ISO 时钟与三时区定向测试策略 | 本次不改变 CI 环境 |
| #402 S1：客户搜索防抖 + Abort | F:142 | 未完成防抖/Abort；迟到覆盖已有 alive 防护 | `PurchaseAnalyticsPage.tsx:99–108` 每次 query 请求，cleanup 设置 alive=false | 建议单独 PR，300ms/请求取消/选项端点须一起验证 |
| #402 S2：抽屉 push 打开/关闭返回对称，深链例外 | F:143 | 未完成 | 采购 `:273`、销售 `:249` 关闭均 patch 默认 replace | 建议单独 PR，覆盖 Back/Forward 与深链历史 |
| #402 S3：PR 正文旧 SHA/计数 | F:144 | 已完成外部正文修正 | 来源明确 2026-10-18 body-only PATCH 对齐 head `3217d1c`；已合 #402 `cc899c8` | 不改历史 PR |
| #402 S5：客户搜索 name_raw / name_normalized | F:146 | 待决策是否统一结果口径 | 采购 `purchase_analytics.py:405–411` name_raw；销售 `sales_analytics.py:100` name_normalized | 可能改变匹配集，待金臣；不静默统一 |
| #402 S5：无客户权限与无结果文案区分 | F:146 | 已完成 | `FilterBar.tsx:65/67/71/111` 区分“客户信息不可见”并禁用与“无匹配客户”；`PurchaseAnalyticsPage.tsx:102/109` 有 customerRestricted 分支 | 仅登记 |

## 数据、发布记录与排除项

| 条目 | 来源 | 判定 | 证据 | 本 PR 处理 |
| --- | --- | --- | --- | --- |
| 采购 11 单行合计 ≠ 单头根因 | F:47；P:21、126；H:482 | 读侧已完成；清源数据需金臣单独决策 | `~/workspaces/reviews/client-req-20261016/purchase-line-mismatch-11.md:22–34`：旧批次残留，9 单重复行号、2 单旧行号消失；D-39:28；详情查询 `purchase_analytics.py:847` 限单头批次 | 只登记；不清源数据、不改数据迁移、不改 loader |
| PR-5 数字 76/10,591 → 122/181,855/110,570 | F:153；P:40–42；H:483 | runbook 已标旧数字失效，但新历史观测未完整回填 | 回填 runbook:86–89；F:153 解释 46 文件 dimension=A1:A1 漏读；D-40:21、28 已更正事实 | 纯文档补 2026-10-18 staging / 10-19 dry-run 观测；不代定新金额验收基线 |
| archive_missing 批次 2/13/14/15 | P:12；H:484、653–662 | 缺原件仍待找回或书面接受缺口 | H 记录 126 批次、4 缺件、tax_unresolved 75,537 字段约 25,179 单、conflicts 0；回填 runbook 已列批次但未完整记影响 | 补文档；待金臣/原件，不执行回填 |
| PR-5 生产 exec 结果 | H:36、659 | 本任务未知 | 本地交接只记 2026-10-19 10:07:22Z 已启动；不访问 ybwznt 核实 | 明确“待生产 exec 完成后回填”，不写成已完成 |
| #425 工作簿项目映射 `.get()` + WorkbookError | U | 由 #430 覆盖，尚未合入本基线 | 只读 gh：#430 OPEN，head `5de17af` 含 `4683754`；该提交 master_workbook:1305–1311；`test_maintenance_workbook_input_errors.py:16` | 不重复实现、不改这些文件 |
| #425 `_v2_decimal` 量化前量级检查 | U | 由 #430 覆盖，尚未合入本基线 | `4683754` master_workbook:3344–3347；同测试文件 `:39–65` 覆盖过大值/NaN/Infinity/舍入越界 | 不重复实现 |
| #425 人工清空后再上传来源测试 | U | 由 #430 覆盖，尚未合入本基线 | `4683754` `test_maintenance_field_protection_source_refresh.py:121` 与 `:285` explicit_clear 参数用例 | 不重复实现 |
| #427 取消连带、#432 ITSP-5a 行级筛选 | U | 不在范围，文件冲突须跳过 | 只读 PR files；涉及 CONTEXT、维保面板、工作簿/行筛选 API 与测试 | 不改交叉文件；本次无业务决策变化，不需改 CONTEXT |
| itsp #393 merge.py dropped-only bump / 30k scale / 契约副本 | F:3–7；P:124 | 不在范围 | 独立 D-36/目录合并轮次 | 不实现 |
| D-36 remaining、shop #63/#64 全部条目 | F:9–25；P:124 | 不在范围 | 另仓库/轮次，含 URL stock 默认、资料筛选、吞吐、空 cursor 脚注、契约交叉引用 | 不实现 |

## 本次验证与交付记录

本次测试 seam 为采购分析公开 summary/orders/order-detail API、采购抽屉用户可见标签，以及 ManualSiteIssueSection CAS 提交前的用户可见按钮；金额期望使用独立常量，验证单头回退与行级结构性规则。后端使用专用 PostgreSQL 15 容器 `itsp-laneB-r1fu-20261020`、随机本地端口与 `PYTEST_DATABASE_BASE_URL`。任一时刻只运行一个测试进程，pytest 不并行，Vitest `--maxWorkers=2`，仅定向子集；不运行本地后端全量。

红绿证据：

- 抽屉新增用例先 **8 failed / 4 passed，6.57s**，失败定位分类合计缺名义标注；补标签后抽屉与采购页 **36 passed，21.98s**，同时检查明细行标签。
- CAS 用例用受控 Promise 稳定复现 API 已调用但按钮未渲染：**1 failed / 21 skipped，7.52s**；改为 `await findByRole` 后，在完整相关前端子集中通过。
- 前端 `tsc → 4 文件 Vitest → vite build`：**85 passed，32.99s**；构建 **20.52s**，总退出码 **0**。构建有 chunk > 500 kB 的提示。
- 后端三个相关文件：**141 passed，348.69s**，退出码 **0**；一条既有 Starlette/httpx TestClient 弃用提示。既有公式正确，新增矩阵属于覆盖补强，未声称后端曾有红灯。
- 三时区定向结果：UTC **39 passed / 7.74s**；Asia/Shanghai **39 passed / 7.60s**；America/Los_Angeles **39 passed / 7.96s**；三次退出码均为 **0**。

复现命令（依次执行，每条结束后再启动下一条）：

```bash
# backend/；仅本任务容器的随机端口，重新创建容器后以 docker port 的结果为准
PYTEST_DATABASE_BASE_URL=postgresql+psycopg://spareparts:spareparts@127.0.0.1:32885/spareparts_test \
uv run --extra dev pytest \
  tests/test_purchase_analytics_amounts_handcalc.py \
  tests/test_purchase_analytics_api.py \
  tests/test_purchase_analytics_equivalence.py -q --tb=short

# frontend/
npx tsc &&
npx vitest run --maxWorkers=2 \
  src/pages/purchases/analytics/__tests__/OrderDetailDrawer.test.tsx \
  src/pages/purchases/analytics/__tests__/PurchaseAnalyticsPage.test.tsx \
  src/components/maintenance/__tests__/ManualSiteIssueSection.test.tsx \
  src/utils/__tests__/date.test.ts &&
npx vite build

for zone in UTC Asia/Shanghai America/Los_Angeles; do
  TZ="$zone" npx vitest run --maxWorkers=2 \
    src/utils/__tests__/date.test.ts \
    src/pages/purchases/analytics/__tests__/OrderDetailDrawer.test.tsx || exit
done
```

上述命令实际通过 `nohup bash -c` 执行，日志在
`~/workspaces/reviews/lanes-20261020/laneB/r1fu-{drawer-red,drawer-green,cas-red,frontend,backend,tz-*}.log`，不纳入提交。
时间格式化实现本次没有变化，三时区运行用于复核已有实现与本次展示用例。

独立审查由另一只读代理执行（同模型、不同角色，不宣称异构审查）；未发现阻塞项。
已采纳金额断言 `rel=0, abs=0.005`、注释限定未标税率、历史提交引用修正等建议。
基线核查与本次修改无 #427/#430/#432 文件交集，无 schema 或数据迁移。
后端回归后已停止并删除本任务专用容器 `itsp-laneB-r1fu-20261020`，没有操作其他容器。

## 改动文件与建议 PR

| 文件 | 本次变化 |
| --- | --- |
| `backend/app/services/purchase_analytics.py` | 仅修正模块说明与名义侧注释，金额表达式不变 |
| `backend/tests/test_purchase_analytics_amounts_handcalc.py` | 60 格公共 API 矩阵、6 格权威零值边界、金额分级精度断言 |
| `frontend/src/api/purchaseAnalytics.ts` | 占比注释涵盖单头缺列翻转 |
| `frontend/src/pages/purchases/analytics/OrderDetailDrawer.tsx` | 行级与分类合计的分侧标签；空金额不标、零金额仍标 |
| `frontend/src/pages/purchases/analytics/__tests__/OrderDetailDrawer.test.tsx` | 新增 12 项展示用例 |
| `frontend/src/pages/purchases/analytics/__tests__/PurchaseAnalyticsPage.test.tsx` | 原单头标签断言限定单头作用域，保留原断言 |
| `frontend/src/components/maintenance/__tests__/ManualSiteIssueSection.test.tsx` | CAS 用例等待真实按钮并用受控响应固定竞态 |
| `docs/releases/2026-10-17-sales-reconciliation-backfill-runbook.md` | 新历史数字、缺档影响、未知生产 exec 状态 |
| `docs/plans/r1-followups-remainder.md` | 本清单、证据与后续边界 |

建议标题：`fix: 补齐采购名义侧展示并核对 Round1 遗留`

建议正文：

> 决策: D-39
>
> 采购分析单头名义侧及缺列翻转已符合 D-39。本次补齐抽屉明细行、分类合计的分侧名义标签，新增 60 种税率/标记/缺列组合及 6 种零值公共 API 覆盖，修正旧注释；稳定化 ManualSiteIssueSection CAS 测试等待。
>
> 逐条核对 Round1 followups，并补 PR-5 历史观测数字。排序契约、防抖/Abort、抽屉关闭历史对称性建议独立 PR；真实 0%、Excel 时间分布、客户搜索列统一、11 单源数据清理等保留待决策/待只读核对。
>
> 验证命令与结果见本清单。无迁移、无生产操作；开发回归通过后待用户点按验收。全量由 CI 执行。

提交只落本分支，候选 SHA 以本轮交付回执与 `git log -1` 为准；不推送、不开 PR、不合并、不部署。
本次开发验证已通过，状态为**待用户点按验收**；未运行全量套件，未做 Safari 实机验证。
