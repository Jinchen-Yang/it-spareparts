# 全模块前端接入与权限中心下一阶段地图

状态：可实施的目录草案与源码接缝审查，**未改实现、未冻结新增业务默认授权、未宣布全覆盖**。审查提交 `42ce3532a886fa137a5d73058c7b73279a4135ed`，实时时间核验 `2026-10-23 15:33 UTC`。用户已要求按既定方案完成所有模块、每阶段审查测试和 CI 后继续，最终统一验收，绝不上生产；本文件不构成生产授权。

依赖：[D-0026](../../decisions/0026-模块权限引擎与客户归属.md)、[D-0027](../../decisions/0027-全模块权限迁移与统一授权编排.md)、[主方案](../../plans/2026-10-23-authz-modular-rewrite.md)、[全覆盖补充](../../plans/2026-10-23-authz-coverage-and-frameworks.md)、[首批契约](../../plans/2026-10-23-authz-implementation-contract.md)、[全模块契约](../../plans/2026-10-23-authz-full-migration-contract.md)、[执行账本](../../plans/2026-10-23-authz-full-migration-execution.md)、[前端接缝](frontend-implementation-seam.md)。业务语义继续遵守 `CONTEXT.md` 与 `docs/decisions/`，尤其汇总/详情范围例外、真实领用、累计实收、工作簿事务和提交即生效。

## 1. 基线结论与计数边界

- 现行 `nav.tsx` 有 **22 个菜单页、2 个详情页、30 个旧地址重定向**。旧 `PAGE_KEYS` 有 **17 个**，不等于模块数量，也不足以形成新目录。
- [frontend-inventory.csv](frontend-inventory.csv) 的 781 行是旧基线入口索引：24 route、30 redirect、98 local_gate、130 role_reference、408 ui_action、63 tab_or_panel、20 export_download、8 global_gate。`ui_action` 中有关闭、重试等纯交互，`tab_or_panel` 中有详情字段，不应按行生成权限。
- [panel-data-surface-inventory.csv](panel-data-surface-inventory.csv) 的 **2,829 个源码出现项不是 2,829 个权限门**。其容器、列、卡片、tooltip、动态模板和复用组件相互重叠；9 个已人工追溯样本、7 个局部引用、2,813 个待语义映射不能解释为无保护数量。本次按业务能力归并目录，不把剩余条目标成已完成。
- 两份索引源于首批实现前基线。本次人工补看 `AuthzProvider`、`api/authz.ts`、`SalesAuthorizationDrawer`、账号页以及 App/nav 新接缝；不把旧 CSV 的行号当作新提交的精确行号。后续实施覆盖检查需在各阶段提交重建索引并以文件/稳定标识匹配。
- `42ce353` 只有 `sales_analysis` 注册到 `RESOURCE_DEFINITIONS`，动作仅 `read/export`。它已完成资源链路首批接入，但**并没有独立的 overview/groups/trend 等面板展示授权**。其他模块全为 legacy，不能把入口改读快照就标 migrated。
- 当前 `/authz/catalog` 由销售资源加其余 16 个 `page_*` 派生，漏掉无页面键的池管理、系统设置、共享文件/工具和维保子域。下一阶段必须改为显式闭合目录，不能继续从 `PAGE_KEYS` 自动推导完整性。

## 2. 主干与登记模型

```mermaid
flowchart LR
  M[闭合模块目录 Manifest] --> G[模板快照 + 账号 Grant]
  R[稳定客户及项目关系] --> E[Cerbos + 业务范围/字段适配]
  G --> E
  E --> S[当前主体能力快照]
  E --> Q[API / SQL / 工具 / 文件投影]
  S --> U[导航 / 路由 / 面板 / 动作]
  Q --> U
```

登记单位分四层：

| 概念 | 解决的问题 | 上游依赖 | 下游调用 |
|---|---|---|---|
| module/resource | 定义可以闭合迁移的业务边界 | 业务对象及读写服务 | 目录、Grant、后端执行器 |
| surface | 某页、页签、卡组、图表/明细区是否可见 | 模块/展示授权及必要数据能力 | 导航、路由、Boundary |
| action | 明确读取、修改、导出、原件下载等操作 | 资源和对象状态 | API、工具、任务、按钮 |
| field group / scope | 哪些对象中的哪些信息可被观察和使用 | 具名关系及派生依赖 | SQL、字段投影、筛选排序、文件 |

稳定命名建议沿用已有 `sales_analysis` 资源名，避免首批重命名迁移；surface 用 `sales_analysis.overview`、`maintenance_project.expense` 等业务标识。一个语义面板在多处出现可共用信息能力；同一 React 组件用于两个业务域时必须分别绑定业务实例，不能按组件名自动授予。

每个 surface 需登记 `id/module/route/component`、`display_policy`（独立或明确继承）、`data_resources/actions`、`scope_rule`、`field_groups/derived_dependencies`、`filter_sort_group_uses`、`exports/files/tools`、`tests`、`migration_status`。纯布局、关闭/重试/分页/颜色主题标为 `presentation_only`，业务数据及具有选择信息的筛选不得自动归入此类。

## 3. 17 个旧页面键与全部现行路由

表中候选资源是下一阶段实现目录；不是给存量账号自动开通的权限。

| 旧页面键 | 当前路由/入口 | 新资源/展示边界与接入文件 |
|---|---|---|
| `page_parts` | `/parts` | `parts_catalog` 搜索/身份；采购历史、销售历史、库存、池与替代料区分别取对应资源决定。`PartSearchPage.tsx`、`components/parts/InlinePartEditModal.tsx` |
| `page_purchases` | `/purchases/analysis`、`/purchases/exceptions`、`/purchases/records` | `purchase_model_analysis`、`purchase_exceptions`、`purchase_records` 三个独立 read；`pages/purchases/{PurchaseAnalysisPage,PurchaseExceptionsPage,PurchaseRecordsPage}.tsx` |
| `page_profit` | `/profit` | `profit` 的 read/export/recompute；`ProfitPage.tsx` |
| `page_inventory` | `/inventory` | `inventory` 查询/候选/修正/撤销修正；`InventoryPage.tsx` |
| `page_chat` | `/chat` | `agent` 对话、会话、运行、附件、业务工具；`ChatPage.tsx` |
| `page_import` | `/import` | `imports` 按文件业务类型分 upload/preview/apply/job_read/errors_download；`ImportPage.tsx`、`pages/import/*` |
| `page_governance` | `/governance` | `governance` 与 `data_quality`，八个页签分别登记；`GovernancePage.tsx`、`pages/governance/*` |
| `page_master_data` | `/master`，另有型号全景内联编辑 | `parts_master` read/create/update/batch_preview/batch_apply/history；`MasterDataPage.tsx`、`BatchNormalizeModal.tsx`、`InlinePartEditModal.tsx` |
| `page_maintenance` | `/maintenance`、`/maintenance/analytics`、`/maintenance/dashboard`、`/maintenance/demands`、`/maintenance/projects/:projectId` | 第4节 B15–B27 子域；不能把一个 page 位复制成全部写权限 |
| `page_boss_board` | `/boss` | `business_board` 销售/采购/KPI/价格纪律/池/排名面板；`BossBoardPage.tsx`、`pages/boss/*` |
| `page_pool_analysis` | `/pools`、`/pool-analysis/:groupId` | `pool_analysis` 列表/详情、采购/销售价格区；`PoolsPage.tsx`、`PoolAnalysisPage.tsx` |
| `page_maintenance_boss` | `/maintenance`、项目详情入口 | `maintenance_projects` 目录及 `maintenance_board` 汇总。D-25 的全范围汇总不自动变为全项目详情 |
| `page_maintenance_beta` | 无独立菜单，多个管理/回款提醒/工作簿子流程 | 把部署可用性、实名/白名单及业务 action 分开登记；`maintenancePermissions.ts` 不继续充当浏览器政策引擎 |
| `page_replenishment_beta` | `/maintenance/replenishment` | `replenishment` read/create/review 与版本/购物车/证据/导出；`ReplenishmentBetaPage.tsx`；部署 Beta 总闸独立 |
| `page_accounts` | `/accounts` | `accounts`、`templates`、`authorization` 三组资源。查看账号不等于改权；`pages/accounts/*` |
| `page_sales_analysis` | `/sales/analytics` | 已注册 `sales_analysis`，继续补 surface 目录、逐单敏感区和统一管理；`pages/sales/analytics/*` |
| `page_purchase_analytics` | `/purchases/analytics` | `purchase_analytics` 汇总/分组/订单明细/订单抽屉；`pages/purchases/analytics/*`。当前无独立导出接口，不凭页面键虚构 export |

另两个菜单必须显式登记：`/pool-management` → 池目录/价格治理资源（现由 `action_pool_manage/action_pool_set_policy` 任一准入）；`/system-settings` → `display_policy`（现为 admin 硬门）。共同覆盖 24 条业务路由。登录、改密、退出、健康检查和 API 文档另列平台边界；AppShell 的“完整日志”实际渲染静态 `CHANGELOG` 更新说明，是呈现例外，不是敏感操作审计。

30 个旧地址全部指向现有目标 surface：`/purchases`、`/sales/replenishment-beta`、`/maintenance/legacy` 和 27 个历史维保地址。重定向必须使用**目标新决定**；无权目标显示拒绝，不由旧页面键再次放行。两个详情路由需对象级决定，不能因父菜单可见而认为任意 `projectId/groupId` 可读。

## 4. 与原32业务组对齐的封闭模块、面板与动作草案

以下 B01–B32 保留主方案的32业务归并，不声称只有32个新资源或权限。每行只有在页面、下列面板、API/SQL、文件/工具及反例均闭合后才能迁移状态变为 migrated。所有新 ID 都需由服务端目录导出给前端；不存在的操作不得生成默认授权。

| 组 | 对齐后稳定资源ID | 语义面与动作索引 | 前端接入处及闭合约束 |
|---|---|---|---|
| B01 经营看板 | `company_pool_analysis`、`company_pool_order_facts`、`company_purchase_orders`、`company_sales_orders`、`company_summary`、`price_discipline` | 各资源数据动作及show依赖见JSON；本组11个语义面，不能合并授权 | `BossBoardPage`、`boss/{OrdersBlock,PartsTable,PoolsBlock,RankingBlock,MorningDisciplineSummary,shared}、PoolOrderDetailModal`；KPI 统计只受时间筛选的现行口径保留；旧 rebuild 恒410退役 |
| B02 销售分析 | `sales_analysis` | 各资源数据动作及show依赖见JSON；本组12个语义面，不能合并授权 | `SalesAnalyticsPage` 及 Summary/Overview/Compact/Groups/Trend/DrillTable/DetailDrawer/AmountHistogram；候选、金额筛选、排名、tooltip 与导出同范围 |
| B03 型号查询 | `part_lookup`、`part_master`、`pool_price_analysis` | 各资源数据动作及show依赖见JSON；本组14个语义面，不能合并授权 | `PartSearchPage`、共享型号全景；目录许可不自动附带交易历史、价格建议或库存 |
| B04 利润分析 | `profit_analysis` | 各资源数据动作及show依赖见JSON；本组3个语义面，不能合并授权 | `ProfitPage`；重算独立于read；客户维度和导出继承相同范围和字段 |
| B05 采购日期分析 | `purchase_analysis` | 各资源数据动作及show依赖见JSON；本组5个语义面，不能合并授权 | `PurchaseAnalyticsPage`、SummaryView/DrillTable/OrderDetailDrawer/FilterBar；三金额及关联客户分开控制 |
| B06 采购型号分析 | `purchase_records` | 各资源数据动作及show依赖见JSON；本组4个语义面，不能合并授权 | `PurchaseAnalysisPage`、`purchases/shared`；趋势、谈价建议、金额排序按成本派生保护 |
| B07 采购异常与明细 | `purchase_records` | 各资源数据动作及show依赖见JSON；本组4个语义面，不能合并授权 | 两个页面不继续用同一开关；候选、供应商筛选与total防侧信道 |
| B08 库存 | `inventory` | 各资源数据动作及show依赖见JSON；本组2个语义面，不能合并授权 | `InventoryPage`；仓库选择只是筛选，与授权仓库范围取交集；保留原因与版本约束 |
| B09 池分析与管理 | `pool_catalog`、`pool_order_facts`、`pool_price_analysis`、`pool_price_policy` | 各资源数据动作及show依赖见JSON；本组13个语义面，不能合并授权 | `PoolsPage`、`PoolAnalysisPage`、`PoolManagementPage`、`components/pools/*`、`charts/PoolPnPriceMap`；成员管理与约束价分开，交易历史按域验权 |
| B10 备件主数据 | `part_master` | 各资源数据动作及show依赖见JSON；本组5个语义面，不能合并授权 | `MasterDataPage`、`BatchNormalizeModal`、`InlinePartEditModal`；单条、批量、内联统一实名和能力 |
| B11 替代关系 | `part_substitute` | 各资源数据动作及show依赖见JSON；本组2个语义面，不能合并授权 | `PartSearchPage` 替代料区；添加/解除不得继续只用角色字面量；库存合计也是独立信息来源 |
| B12 治理 | `governance`、`part_identity` | 各资源数据动作及show依赖见JSON；本组6个语义面，不能合并授权 | `GovernancePage`；预览与应用同对象集；现有admin或页面附带写资格逐动作保留后显式迁移 |
| B13 疑点与校准 | `data_quality`、`data_quality_calibration` | 各资源数据动作及show依赖见JSON；本组2个语义面，不能合并授权 | `DataQualityIssuesPanel`、`DataQualityCalibrationPanel`；必要证据与成本条件，校准只读不生成业务状态 |
| B14 通用导入 | `data_import` | 各资源数据动作及show依赖见JSON；本组4个语义面，不能合并授权 | `ImportPage`、ImportPrecheckPanel/ExpenseVoidPreviewPanel；`Object.entries(detail.report)`禁止未经字段白名单直出；任务执行和下载重验 |
| B15 维保目录 | `maintenance_project_cards`、`maintenance_project_directory`、`maintenance_summary` | 各资源数据动作及show依赖见JSON；本组7个语义面，不能合并授权 | `MaintenanceHomePage`、现行ProjectCard、MaintenanceProjectExportButton；旧MaintenanceProjectCard/BossProjectTable/PeriodDeltaCards/AttentionList/SourceHealthBar暂不可达，不能冒充现行面板；目录候选见ReturnReceiptsSection与DemandLineCreate/BatchCreate |
| B16 维保分析/看板 | `maintenance_analysis`、`maintenance_contract_cost`、`maintenance_cost`、`maintenance_dashboard` | 各资源数据动作及show依赖见JSON；本组16个语义面，不能合并授权 | `MaintenanceAnalyticsPage`、MaintenanceRankingsExplorer、MaintenancePnCompact*、MaintenanceDashboardPage、dashboard/*；数量、实收、成本、试算三域分开，不能因选择某tab读取全部 |
| B17 项目基础及关系 | `maintenance_assignments`、`maintenance_project`、`maintenance_project_operations`、`maintenance_source_assignment` | 各资源数据动作及show依赖见JSON；本组7个语义面，不能合并授权 | `MaintenanceProjectPanelPage`、OverviewTab、ProjectManagerAssignmentControl；稳定project_id，归属编辑是授权影响操作，名称不作为权限绑定。后端现存手工建项入口与D-05冲突，需独立收口，不能声称接口不存在或已封 |
| B18 需求与同步 | `maintenance_demand`、`maintenance_recovery`、`maintenance_source_document` | 各资源数据动作及show依赖见JSON；本组10个语义面，不能合并授权 | `MaintenanceDemandsPage`、DemandLine*、DemandLinesEditor、WbddReconciliation/UploadFeedback、SourceCancelImpactPanel；作废恢复独立；批量仍逐行编排，不能宣称跨行原子 |
| B19 现场领用 | `maintenance_cost_fill`、`maintenance_issue`、`maintenance_project_purchase` | 各资源数据动作及show依赖见JSON；本组6个语义面，不能合并授权 | `SiteReturnTab`、SiteIssueWorkflowPanel、ManualSiteIssueSection、SiteIssueEditDrawer；对象范围、确认状态与版本继续服务端校验 |
| B20 返还/实收/残值 | `maintenance_front_stock`、`maintenance_recovery`、`maintenance_return_import`、`maintenance_return_obligation`、`maintenance_return_receipt`、`maintenance_salvage` | 各资源数据动作及show依赖见JSON；本组10个语义面，不能合并授权 | `BadReturnPanel`、ReturnReceiptsSection、ReturnReceiptBatchEntry/Maintenance、SiteIssueReturnBatch、SalvagePanel、RecoverySummaryPanel；稳定需求关联、当前累计容量、金额与原件分别验权 |
| B21 合同/报销/回款 | `maintenance_collection`、`maintenance_contract`、`maintenance_expense`、`maintenance_expense_reconcile`、`maintenance_project`、`maintenance_project_operations`、`maintenance_project_purchase` | 各资源数据动作及show依赖见JSON；本组11个语义面，不能合并授权 | 项目页HealthBand、ExpenseTab/CollectionTab/CollectionBatchMaintenance；旧ContractPortfolio/ProjectFinancialProgress暂不可达；合同额、成本、报销、回款按各资源字段政策，复合workspace不得借summary.read放行 |
| B22 回款提醒/证据 | `maintenance_collection_followup`、`maintenance_collection_plan_import` | 各资源数据动作及show依赖见JSON；本组3个语义面，不能合并授权 | CollectionReminderDetail、CollectionMilestoneFollowUpModal、CollectionPlanImportModal；部署可用性与当前实名白名单限制仍显式记录，不用admin自动绕过 |
| B23 工作簿/行工作台 | `maintenance_cost`、`maintenance_cost_roundtrip`、`maintenance_finance_workbook`、`maintenance_general_workbook`、`maintenance_global_rows`、`maintenance_global_workbook`、`maintenance_managed_workbook`、`maintenance_manager_workbook`、`maintenance_project_roundtrip`、`maintenance_project_transfer`、`maintenance_row_workbench` | 各资源数据动作及show依赖见JSON；本组15个语义面，不能合并授权 | WorkbookRoundTrip、ProjectWorkbookActions、MaintenanceBatchTransferButton、GlobalMaintenanceRowWorkbench、ProjectMaintenanceRowFilters、各tab；一个复合文件必须覆盖所含域/字段；保留D-03负责人完整工作簿例外，不从局部网页scope推导 |
| B24 维保导入生命周期 | `maintenance_ai_assist`、`maintenance_document_import`、`maintenance_ledger_import`、`maintenance_project_transfer`、`maintenance_return_import`、`maintenance_return_receipt`、`maintenance_shipment_import`、`maintenance_source_document` | 各资源数据动作及show依赖见JSON；本组14个语义面，不能合并授权 | ReturnReceiptImport、MaintenanceBatchTransferButton及各导入组件；页面隐藏不代表API退役，所有上传、后台执行、结果和原件路径纳入 |
| B25 验收/附件/清单 | `maintenance_acceptance` | 各资源数据动作及show依赖见JSON；本组3个语义面，不能合并授权 | AcceptanceTab、MaintenanceAcceptancePanel；现行提交即生效，历史approve/reject不可重新启用；文件父项目范围不能只按file_id |
| B26 仓库/迁移/对账 | `maintenance_migration`、`maintenance_warehouse` | 各资源数据动作及show依赖见JSON；本组2个语义面，不能合并授权 | 当前可达发货候选/批量裁决接缝；无独立导航的服务仍登记。双人条件、系统启用总闸和生产激活控制独立于grant |
| B27 补库/购物车 | `replenishment_request` | 各资源数据动作及show依赖见JSON；本组4个语义面，不能合并授权 | `ReplenishmentBetaPage`；实际只登记当前一次性提交/购物车/修订/system-screening导出；旧review/evidence等11个handler固定410，不提供活跃授权，AI不写状态 |
| B28 AI/会话/文件 | `agent_chat`、`agent_file`、`private_chat_session` | 各资源数据动作及show依赖见JSON；本组3个语义面，不能合并授权 | `ChatPage`；稳定实名owner；工具不从chat.use推导数据权；工具摘要、错误、markdown和附件也按字段及范围投影 |
| B29 账号/模板/授权 | `account_directory`、`account_lifecycle`、`authorization_admin`、`authorization_snapshot`、`legacy_template` | 各资源数据动作及show依赖见JSON；本组11个语义面，不能合并授权 | `pages/accounts/*`；本阶段仍限实名管理员改新授权，普通action_account_manage不隐式升级为authorization管理；委派将来独立定义 |
| B30 设置/审计 | `account_directory`、`display_policy`、`maintenance_audit` | 各资源数据动作及show依赖见JSON；本组5个语义面，不能合并授权 | `SystemSettingsPage`、TaxBasis、账号活动、实收历史；业务设置读取不能暴露平台密钥或任意服务控制。AppShell静态更新日志不属于操作审计 |
| B31 机器集成 | `resale_catalog`、`resale_price_reference`、`resale_return_feed` | 各资源数据动作及show依赖见JSON；本组3个语义面，不能合并授权 | 无员工页面；服务主体的返件feed、目录与参考价合同，保持service_only，不能继承员工模板或被人类页面键误改 |
| B32 认证/平台例外 | `authentication`、`framework_documentation`、`health_probe` | 各资源数据动作及show依赖见JSON；本组5个语义面，不能合并授权 | LoginPage/ChangePasswordModal/LoginChangePasswordModal、API会话层；公开例外需显式原因，禁止未知路径默认允许 |

跨 B14/B22–B25/B28 的附件与任务使用统一安全适配器，但**授权依据沿父业务资源**，不创建“全站文件可读”宽泛许可。后台存在、前端暂不可达的端点需在后端清单保留状态，不为它们强造新菜单。

## 5. 必须独立登记的敏感展示面与信息组

“独立登记”包含展示开关及数据能力归属；不是要求每个表格单元格成为复选框。以下信息不能仅继承“能进入页面”。独立展示关掉时不挂载请求组件；数据能力关掉时还要收紧 API/SQL/导出/工具/排序筛选。

| 信息组/面板 | 必须包含的展示及派生信息 | 独立性理由与实现约束 |
|---|---|---|
| 客户身份/归属/联系资料 | 客户名、客户数/排名、关联销售单、联系人/电话/地址、归属名单 | 与负责客户scope不同：能限定到某客户不等于可看全部联系信息；需求单/领用地址组件及导出均登记。现有data_customer不可未经决策自动等价于所有个人联系字段 |
| 供应商及采购关系 | 供应商名、供应商候选、采购明细、按供应商total与排序 | 隐藏名称仍允许筛选会泄露关系；页面和AI采用同一查询用途规则 |
| 金额/成本与公开营收 | 采购含税/未税/实付、销售金额、单位成本、估算证据、覆盖率、单均金额副行 | 首批沿用data_purchase_cost及D-44营收规则；新目录可按域细分但不能自改默认。数量卡的金额副行不能混入纯数量面板 |
| 利润/经营诊断 | 毛利额/率、赚钱亏钱榜、成本率、告警颜色、贡献毛利、谈价建议 | 收入/成本/利润可相互反推；禁利润时不能凭剩余字段重建。颜色、排名、建议文本需与数值同政策 |
| 销售对账 | 已/待对账、退货已对账、快照订单金额、截至时间和历史快照 | 已有 `data_reconciliation → data_purchase_cost` 依赖；不能仅隐藏“对账”页签却保留快照金额 |
| 池价格治理 | 采购上限、销售下限、越线差额/比例、覆盖率、价格纪律订单 | 与一般池身份/成员read分开；`policy_set`写与治理信息read分开，隐藏字段不可用于排序筛选 |
| 维保合同与回款 | 合同额、回款计划/实收、剩余款、完成状态、提醒/水位/证据 | 当前常复用profit，目标分别登记字段/动作，先按旧实际行为迁移；项目基础read不能自动给财务数据 |
| 维保报销/成本 | 费用事实、费用完整水位、领用成本与取价证据、成本进度 | 明细、汇总、图、报表与复合工作簿一致；“分析定位”不能显示为项目全量经营值 |
| 实收/残值试算 | 数量核对、领用、实收、坏件市场试算、成本域估值、覆盖/未估量 | 数量与金额分开；采购/销售/领用成本三域独立，hidden金额不经tooltip/百分比/排序间接泄露 |
| 原件/审计/导入报告 | 原Excel、问题文件、before/after JSON、原始报告、验收/回款附件 | 结构化表已遮蔽不代表原件可读；动态JSON必须服务端白名单和字段投影，审计本身要独立能力 |
| 账号与关系配置 | 账号活动、销售身份、客户/项目归属、模板与Grant差异 | 只读账户查看与管理、授权诊断、改变关系必须分开；目标账号诊断不得写入当前操作者Provider |
| AI内容/文件 | 工具原始结果、工具卡摘要、Markdown/链接、持久会话历史、下载 | 每个工具复用域授权；“不让模型看到”与前端收起卡片不同；撤权后的历史内容读取需明确同源策略 |

纯切图类型、折叠、排序方向按钮、搜索草稿的打开/关闭通常不是新能力；它们提交的字段用途仍需后端许可。全选只能选择目录明确授予的子项，不能通过前端自动补依赖扩权。

## 6. AuthzProvider、路由与页面的统一接入

保留现有Provider的token归属、sequence、abort、跨会话迟到响应拒绝、focus/visibility刷新、失效事件。将当前销售专用结构推广为服务器目录驱动的 `modules/resources/surfaces`，无需引入另一套浏览器策略库。

| 接缝 | 本次核实的现状 | 下一步修改责任 |
|---|---|---|
| `api/authz.ts` | sales_analysis、6字段、两scope和管理写端点固定 | 协议schema/catalog版本、闭合模块ID、允许的动作/字段/范围由服务端返回；未知项失败关闭 |
| `authz/AuthzProvider.tsx` | 会话隔离可复用；Unavailable文案只写销售 | 通用状态；未知模块、加载/故障/暂停都不能fallback旧允许；保留会话生命周期测试 |
| `nav.tsx` | 仅销售有authzResource；详情与旧地址仍旧键 | 所有menu/detail/redirect绑定稳定surfaceId；不由admin/role/anyPerm计算已迁模块决定 |
| `App.tsx` | 销售新旧按enabled切，其余localStorage；详情始终旧门 | 按显式migration/runtime状态选择权威；已迁暂停只拒绝，首次未切换legacy才用旧链路；已知无权路径显示拒绝 |
| `AppShell.tsx` | allowed驱动菜单和预取，可复用 | 只对allow入口预取；静态CHANGELOG是呈现例外；角色保留展示用途 |
| 页面useEffect/useAsyncData | 大量父页先请求全模块再渲染tabs | 拆读请求或返回已投影聚合；Boundary必须在有副作用组件外。隐藏tab不能继续发无权请求 |
| `maintenancePermissions.ts`、boss/shared、PartSearch局部读权限 | role/perms本地推算，多处useState只取一次 | 已迁模块消费当前query/object决定；兼容函数只可返回旧模块legacy决定，不能与新许可OR合并 |
| 业务API对象响应 | 部分已有capabilities/amounts_visible | 统一版本与对象上下文；决定绑定主体、project/order/file稳定ID及版本；一项目allow不得复用给另一项目 |

全局快照只声明范围内可用能力，不能装入全部项目/客户数据，也不能输出可执行SQL。项目上下文允许 `requires_context`，取得对象响应/批量决定前相应动作不可用。对象状态校验、数量上限、幂等、版本锁仍由服务端执行，不成为前端授权表达式。

## 7. 账号、职位模板与范围关系的统一编排

### 7.1 需要替代的双轨结构

`AccountDrawer` 用 `template_perms + overrides` 计算旧布尔图；`TemplateDrawer` 保存旧 `permissions`；`SyncModal` 做旧键diff。`SalesAuthorizationDrawer` 独立写 `{actions,scope,field_permissions,customer_ids}`，正确隔离单账号/会话与409，但不会随模板变化。现有提示明确两套不同步，属于首批边界，不能在全模块验收时仍保留此双轨体验并称“统一模板”。

统一使用一个 `ModuleGrantEditor`（建议组件名）供单账号、模板、批量预览复用；沿用 AntD Drawer/Tabs/CheckboxMultiSelect，不复制每模块一个SalesDrawer。管理UI按业务域分组，默认显示读/写/导出/范围/信息组摘要，再展开面板展示；隐藏纯布局与已退役操作。

### 7.2 编排契约

| 界面/服务动作 | 输入输出与规则 |
|---|---|
| 读取目录 | module/resource/surface/actions/合法scope/fields/依赖/migration_state/runtime_state/catalog_version；未迁项明确旧来源，可查看，不伪装为新Grant已生效 |
| 读取账号 | 稳定账号ID、grant集合、模板快照版本、个别allow/deny或override、关系版本、最终决定和安全原因。配置值与实际生效值分别展示 |
| 编辑账号 | 动作、scope与字段成组保存。新模块不继续提交49键布尔diff；一个模块多个范围/字段组合不能被摊平后做笛卡尔积 |
| 模板保存 | 保存版本化grant包；保留“仅保存模板”不会修改存量账号。模板base_role只作兼容标签，不能继续承载未展示的数据范围规则 |
| 模板同步/批量 | 复用现有dry-run→fingerprint→execute交互，但diff包含新增/撤销能力、scope扩缩、fields变化、surface、版本、需重登。scope或field变化必须显示，不能仅报开关变化 |
| 账号覆盖 | 保留“来自模板/单独开启/单独关闭/已过时快照”；默认同步保留个别调整，清除必须显式选择。服务器重新计算有效决定，前端不能自行合并出allow |
| 提交与并发 | 提交带catalog/policy/account/template/grant/relations版本与预览指纹；服务端锁内复验、审计、token_version撤销；409保留草稿并要求重载，不用旧预览重放写入 |
| 操作者权限 | 目前D-0026只允许实名管理员管理新授权；`page_accounts`或旧account_manage不能自动成为新委派能力。只读者不挂载可提交写表单 |
| 自身修改 | 复用销售抽屉已有自改登出与当前会话失效；目标诊断结果独立缓存，绝不能更新操作者Provider |

前端不自动添加金额/范围依赖来“修好”非法组合。显示服务器目录定义的依赖与影响，由管理员显式确认草稿，服务端最终验证。即使一键全选也不能授予目录外能力或越过操作者上限。

### 7.3 客户和项目范围

- **客户关系**：复用 `SysCustomerAssignment(user_id, customer_id)`、远程搜索、已选保留与稳定ID标签。关系是账号共享业务事实，不随模块反复保存；独立“负责客户”区展示共同负责，明确影响全部历史与未来订单。删除/新增关系时预览受影响模块，不按成交销售名猜归属。当前 sales Drawer 把grant与关系同事务保存的安全性质需要保留于新编排，避免两个调用半成功。
- **项目关系**：复用现有负责人/销售关联与稳定 `project_id`，先登记来源和变更语义，不为授权再建一套凭名称的“项目负责人”。`assigned_projects`、`selected_projects`、`all`只能在模块确有后端scope适配时提供；不同信息粒度（目录/汇总/详情/工作簿）分别配置。D-25汇总广于详情及D-03负责人完整工作簿保持具名例外。
- **关系与模板分离**：模板可写“仅负责客户/项目”的规则，不能复制某员工客户ID、负责人映射或项目ID到新员工；显式指定项目集的grant必须在预览中展示具体对象及来源，不能藏在模板名称内。模板同步默认不改变客户/项目归属。
- **空集及未知**：未分配关系是可解释的空范围，不得退回全量；客户/项目已删除或候选搜索无返回不能悄悄丢掉已选ID再扩大scope。前端显示空范围与服务不可用不同状态。
- **跨域对象**：领用/需求/实收/合同/附件关联检查取父子资源实际许可的交集；拿到一个项目read不代表任意关联合同的完整数据都可读。

## 8. 显式迁移状态与阶段顺序

两个状态轴区分“已实现未启用”与“没有实现”。以下采用S1前后端wire对拍命名；最终枚举以后端唯一wire文档为准，早期`legacy/preparing/migrated/platform_exception`与`legacy_active/not_applicable`候选枚举不用于实现。服务/平台例外另属目录分类，不混进员工资源运行状态：

| 状态轴 | 值 | UI与实施含义 |
|---|---|---|
| migration_state | `planned` / `implemented` / `retired` | planned为待实施只读，不能写Grant；implemented表示执行定义已接入，是否启用仍看runtime；retired不授予。目录登记本身不能升级状态 |
| runtime_state | `legacy` / `enabled` / `suspended` | 尚未切换才可legacy；enabled使用新权威，suspended失败关闭且不回退。状态由服务端返回，管理员业务表单不能切部署总闸 |

实施顺序以[主执行账本](../../plans/2026-10-23-authz-full-migration-execution.md)的S0–S4为准，本文件不另建阶段编号：

1. **S0 框架与销售**：当前候选及CI，不把本设计文档计入已完成授权。
2. **S1 通用控制面**：闭合目录、Provider/Boundary、账号/模板/关系规范编排。先让销售使用公共路径，其余资源明确legacy/preparing；控制面接口细节见第12节。
3. **S2 商业与基础域**：B01–B13/B30的商业读写、字段、导出与工具，以及销售各展示面拆门。
4. **S3 维保全部读写域**：B15–B27，保持D-03/D-25及业务事务、工作簿/原件/任务生命周期，每个域同时闭合对应替代入口。
5. **S4 跨域收口**：通用导入B14、AI/聊天B28、机器B31、平台B32及遗留退役核对，完成全量复审与CI后统一用户验收。

实际可并行分工须以共享文件归属分开：核心协议/目录一个负责人；前端壳一个负责人；账号中心一个负责人；业务页按域分。`maintenancePermissions.ts`、`api.ts`、`nav.tsx`、`App.tsx`不可多人同时独立重构。

## 9. 每阶段必须交付的反例与证据

| 层次 | 至少需要的反例/检查 |
|---|---|
| 目录覆盖 | 当前nav 24路由+30重定向全部有surface；所有页面可达文件的新展示有语义归属/纯呈现理由；API与工具有执行映射，不能仅靠关键词出现 |
| 表现 | page允许但敏感panel拒绝时不挂载/不请求；panel显示允许不等于数据allow；同共享组件不同域不串权 |
| 会话 |换账号、跨标签登出、迟到成功/401、焦点刷新、本人改权登出；授权失效后旧draft/对象决定不残留 |
| 字段与派生 | 成本/利润/供应商/客户关闭后原值、tooltip、颜色、排名、筛选、排序、建议、导出和工具结果均验实值；无权限与真实零/无记录区分 |
| 对象范围 | A客户/A项目grant不能读取B；空关系返回空而非全部；合法汇总/详情差异必须有单独反例，不用“集合相同”误伤业务例外 |
| 文件/任务 | upload/preview/apply/download之间撤权或改归属；历史file_id/validation_token/任务ID不能凭旧成功结果绕过当前范围；复合工作簿按所含域校验 |
| 管理 | 模板仅保存不改账号；同步指纹冲突全失败；保留/清除override；scope/fields变化可见；普通account_manage不能提权；关系改动不由模板隐式触发 |
| 业务契约 | 需求作废/恢复、级联、领用确认、实收容量、逐行批量与工作簿单事务、验收提交即生效、AI不写业务状态继续有效 |

每阶段记录精确提交、实现与未接范围、本地有意义回归、独立多角度审查结果、CI结果，再进入下一阶段。源码扫描和测试数量不是用户验收。现有2829索引待语义映射项必须在对应模块迁移时归零或形成明确例外；本草案没有将其批量标为完成。

## 10. 本轮完成与未完成

完成：复用两份CSV并核对当前nav/Provider/账号中心/关键业务组件；明确17页面键、24路由、无页面键模块、32业务组的目录与接入点；列出独立敏感面、模板/Grant/关系编排方案及阶段反例。

未完成：逐2,829源码项最终字段映射、各DTO/SQL/工具完整运行验证、新业务默认授权冻结、任何实现/迁移/部署、浏览器或真实账号验收。本文件是下一阶段施工与审查输入，不是全模块授权已完成的证明。

## 11. 机器草案与资源命名更新

后续机器目录见 [frontend-resource-contract-draft.json](frontend-resource-contract-draft.json)。资源ID与后端审查按真实数据合同重新对齐，第4节已同步最终草案ID；机器草案中的 `resource_alias_reconciliation` 保留早期候选名到当前ID的替代映射，第3节旧页面键只是入口关联。采购三旧页面共用 `purchase_records` 数据合同且各有展示门；新采购分析为 `purchase_analysis`；经营看板拆五资源；池目录、价格政策、价格分析与池订单事实分开；维保拆清目录/卡墙/汇总/详情和工作簿的不同集合。

JSON显式区分当前路由可达、仅有闲置组件、仅后端服务和平台例外。`BadReturnPanel`、`SalvagePanel`、`FrontStockPanel`、部分回款提醒与旧工作簿组件存在于源码并不证明当前页面挂载；不得据表中的接缝候选宣称这些面板已对用户开放。显示动作、业务动作、字段用途分别登记，所有新增许可默认不授予。

进一步核对后的重要修订：

- 现行项目卡是 `ProjectCard.tsx`，旧 `MaintenanceProjectCard`、`BossProjectTable` 和部分汇总组件暂不可达。经营看板订单事实弹窗使用 `PoolOrderDetailModal` 并注入 `fetchDashboardOrderDetail`；旧 `boss/OrderDetailModal` 暂不可达。当前TypeScript静态import闭包已经重算，但它仍不是浏览器运行证明。
- `company_pool_order_facts` 与 `pool_order_facts` 字段合同相同但入口资格不同；不能把两个旧页面键OR成一条授权。
- 完整总表采用 `maintenance_managed_workbook` 与 `maintenance_general_workbook`。同项目选择一条完整合同，不能拼两条的范围、动作或字段。全局行表/全局完整工作簿/经理月度V3/旧单项目四表另建合同。早期笼统workbook别名不得用于注册。
- 旧补库审核及若干旧申请/导出handler共11个固定410，不提供活跃 `replenishment_review` 授权。当前一次性提交、购物车、修订及 `system-screening` 导出按 `replenishment_request` 登记。退役handler列表见JSON，不将残留前端组件恢复成活跃能力。
- `maintenance_audit` 实际是全公司脱敏需求号匹配归因，无项目范围及原始成本；不能与实收before/after历史混淆。后者是 `maintenance_return_receipt.read` 的对象数据面。
- 项目页顶部 `HealthBand` 是跨合同组合：合同额、成本率和备件成本来自 `maintenance_summary`，回款、报销和领用成本来自 `maintenance_project_operations`；后者不能凭前者的read或show动作读取。停用项目的经营行搜索实际还经过卡墙合同。所有分支须按同项目及各自字段合同裁决，禁止把负责人A项目的完整字段借给普通销售可见的B项目。

全模块契约已明确此前提出的四处接口细节：`show_*`独立展示与具名业务数据动作分开强制；快照及对象决定绑定主体/epoch/对象版本；完整资源替换、空动作撤销且保留递增版本、关系事务；旧账号生命周期与批量布尔写入转规范编排或显式退役。JSON的 `contract_review_findings` 已更新为“契约已明确，实施未验证”，具体wire schema与反例仍须实施时闭合。

机器草案现有85个资源描述、189个语义展示/操作面，索引覆盖32业务组、24现行路由、30重定向、17旧页面键；其中包含服务专用及平台例外，不能解读为85个员工权限开关。13个语义面只在源码中存在而未证明当前路由可达。资源与后端草案87合同的差额是两个退役伪合同；未把这些退役入口加入活跃员工授权。上述数量仅说明草案结构和命名闭合，不表示2829源码项逐个映射或运行授权已经完成。

## 12. S1控制面最小可实施UI协议与分工

本节是设计建议，不是已存在API。现行账号中心有两套互不联动的写路径：`AccountDrawer`修改旧模板/布尔覆盖，`SalesAuthorizationDrawer`修改销售规范Grant和客户关系。通用编辑器落地时必须同时处理账号、模板、同步、批量和建号链，不能只换抽屉名称。

根代理已明确模式选择：服务端部署配置返回`authorization_mode`，默认`legacy_compat`保持现行业务兼容；候选`unified_modules`启用后，停止所有旧boolean改权入口，未迁/准备中的资源只读显示“待实施”。此状态不能由前端角色、localStorage或管理员业务表单推导/切换。统一模式出错也不能退回旧写入；新模块编辑器只管理canonical Grant，资料与普通角色分类独立，套模板必须显式操作。阶段内部暂时不能编辑未迁资源是接受的过渡状态，不能因此猜测旧共享data键到新资源的映射。

### 12.1 当前提交路径与必须一起切换的行为

| 接入处 | 当前实际提交 | S1具体变化 |
|---|---|---|
| `AccountDrawer` | `diffOverrides(template_perms, permissions, all_keys)`后`updateAccount({template_code?, overrides})`；当前抽屉没有独立role编辑控件 | 统一模式只读取/写入规范模块包及来源；显式模板应用由服务器处理，未迁资源只读。旧overrides/permissions与role隐式套模板请求由后端整体拒绝，不能因为当前UI无role控件而漏掉 |
| `TemplateDrawer` | create/update旧`permissions`和`base_role`，更新带模板version | 元数据与规范模块包必须在同一模板事务保存。base_role文案改为兼容标签及未迁域影响，不能继续声称它决定新资源行级范围。新建/复制不复制人员客户/项目关系 |
| `TemplatesTab` | 开启数量=`Object.values(permissions).filter(Boolean).length`；停用模板不改已套用账号 | 分别显示已登记可编辑资源、未迁旧配置、待启用配置；不把旧49键或85设计资源当已实现覆盖。保留停用/恢复不追改账号的语义 |
| `SyncModal` | 旧`changed_keys`预览与fingerprint执行；默认保留旧逐键override | 改用服务端逐资源前后配置和来源，显示scope/fields/override；默认保留整资源个人调整。“保存并同步”仍分成模板保存、账号同步两个明确事务，后一事务失败不得声称前一事务未发生 |
| `BulkModal` | `apply_template/grant/revoke/reset_to_template`及旧keys数组；dry-run→fingerprint | 统一模式停止旧keys增减；只允许显式应用模板、替换指定完整资源、撤销指定资源、恢复模板资源。一次预览/一次原子执行，禁止浏览器循环逐账号PUT造成部分成功 |
| `AccountsTab`建号 | 表单默认`template_code=readonly`，建号请求仅含旧模板身份 | 统一模式不预选授权模板，默认新Grant为空。明确选择模板时，在建号事务内实例化已实现资源的规范包；未迁资源不因角色或旧模板默认值生成Grant |
| `AccountsTab`生命周期 | active/password/delete仍走旧账号端点；删除显隐有localStorage角色判断 | 服务器返回操作者能力；身份提升/降级、最后管理员保护及会话撤销由统一服务强制。UI不得把base_role标签当管理员身份，也不得从account_manage推导新授权管理 |
| `PermissionCenterPage` | 加载accounts/meta，展示旧权限与销售授权两入口 | 统一“权限与范围”入口，账号数据请求也增加token/target/sequence隔离；真实实名管理员决定来自服务端。目标账号诊断结果独立于当前操作者Provider |

统一模式对旧`permissions/overrides/keys`改权和旧模板布尔保存/同步整体拒绝，不能静默丢字段、猜映射或返回“成功但新权限不变”。资料保存与普通角色分类保存不修改Grant，旧role触发隐式套模板的分支必须移除或明确拒绝。若某规范操作在S1尚未接通，界面禁用并解释状态，服务器也拒绝；默认兼容模式继续原行为，不因读取新目录自动切换。

这里“分类独立”不能只靠改文案：现有`SysUser.role/Template.base_role`仍被未迁域运行时读取，S1统一模式建议普通role/base_role只读；若需要编辑职位类别，使用与它们分离的纯资料字段。`salesperson_name`还是canonical销售归属输入，也不能归入普通显示名保存；变更须走现行归属事务、版本/会话失效及影响预览。`display_name`等纯标签才可独立保存。管理员升降继续独立受保护命令。

### 12.2 最小读取和写入合同

现有主契约已提供`GET /authz/me`、`GET /authz/catalog`、账号/模板modules读取与版本化写入、模板sync。前端最少需要以下字段和行为，具体wire类型须后端fixture冻结。

| 数据对象 | 必需字段/语义 | 使用者 |
|---|---|---|
| 目录 | schema/catalog version、authorization_mode，资源ID/中文名，migration/runtime状态，合法actions/scope/fields/依赖，display_actions/data_requirements，旧来源与服务端actor capabilities | Provider、导航、通用编辑器、旧配置只读区 |
| 可编辑状态 | 未注册/legacy/preparing只读；已实现且启用才按服务端能力编辑；已实现但暂停时仅在明确支持配置编辑的情况下可写，运行仍deny | 编辑器按钮与保存结果提示；retired/platform/service不进入员工赋权选择，不从目录条目数推导已迁数量 |
| 账号模块包 | 稳定主体ID，username标签，epoch，逐资源version和完整配置，模板来源/快照版本，整资源个人调整，客户关系及其当前版本语义 | 单账号编辑、对比、预览；不能替代当前操作者决定 |
| 模板模块包 | 模板code/version及元数据、已实现完整资源预设、未迁旧配置的只读来源、配置适用状态 | 模板保存/复制/显式应用/批量套用；不含员工客户或负责人关系 |
| 变更预览 | 操作者和稳定目标集合、目录/模板/目标epoch/资源版本、逐资源before/after及来源、关系diff、重新登录影响、fingerprint | 单账号模板选择、同步和bulk；展示服务器结果，浏览器不计算effective授权 |
| 执行结果 | 实际写入对象及新版本/epoch、已失效会话、未改变资源、协议定义的运行状态；失败必须明确是否整个事务未提交。S1不提供staging Grant存储 | 成功提示、目标刷新、当前用户自改登出；不能统一写“全部权限已生效” |

新组件最少四个接缝：纯受控`ModuleGrantEditor`负责配置与依赖错误，`AuthorizationDiff`负责统一预览，`AuthorizationChangeController`负责请求归属/版本/执行失效，`CustomerAssignmentEditor`复用CheckboxMultiSelect和现有稳定ID搜索。它们不自行拼接有效权限。原`PermissionMatrix`仅在默认兼容模式继续编辑；统一模式至多只读展示旧来源，不能作为新资源默认配置生成器。

Provider只消费一次批量快照，不按85个目录描述逐项请求授权。后端批量加载Grant并调用PDP；前端不预加载全部项目集合，详情按钮使用同对象API返回的capabilities。资源“未切换/启用/暂停”分别显示，暂停不等于恢复旧权限；新旧配置冲突需服务端统一拒绝，不能出现前端快照与实际执行的模式不同。

账号/模板所有写入必须带预期版本；未提交资源保持，`actions=[]`写保留版本的拒绝记录。客户关系未提供保持，空数组显式清空。关系变化与相关Grant同一账号事务提交。项目关系第一步展示当前来源、可见范围和现行项目管理链接，继续走既有项目版本事务；目前不存在统一权限中心项目写接口，不能假装已接入。

每次选择模板、目标集合、清除调整选项或资源配置变化，立即清空旧preview。请求携带操作者token快照，结果须同时匹配token、target和sequence；迟到预览不能启用执行按钮。409保留草稿并废弃指纹，用户重新看新预览后才可执行；不自动重放。本人修改继续复用现有销售抽屉的会话清理和失效事件。

### 12.3 Wire对拍结果与独立延期项

具体协议统一见后端负责的[S1 wire contract](s1-wire-contract.md)。前端已完成5份golden、45个独立场景的静态消费对拍；安全/事务独立复审由根汇总。本节是消费索引，早期候选字段名被wire替代，不维护第二套DTO：

1. **Wire schema**：`schema_version=1`，服务端`authorization_mode/catalog_version`，safe integer ID与版本。epoch复用token_version，来源变化也递增，不增设source_revision。计划资源只读；S1不提供staging Grant。
2. **显式模板应用与bulk**：复用`POST /accounts/bulk`，单目标也提交明确targets；dry-run响应返回规范command和fingerprint，执行原样提交规范command，不在浏览器推算影响资源集合。`clear_overrides`恢复各账号保存快照，`sync_template`才追最新模板。
3. **建号/模板原子保存**：`POST /accounts`明确`authorization.kind=none|explicit|apply_template`，UI默认none；账号实际role默认readonly不等于获得规范Grant。模板metadata与规范包按同一version原子保存；统一apply/sync不改仍供未迁域使用的旧role、template_*或perm_overrides。统一UI只展示authorization_profile来源，不能把旧template_code当新来源。
4. **延期且不阻塞S1**：项目关系先使用现有项目版本流程与只读展示；若以后直接在权限中心编辑，再固定expected project version及事务合同。

前三项已由前后端对拍并进入唯一wire稿，前端DTO无剩余阻断。JSON的`control_plane_ui_draft.minimum_open_items`保留历史问题和解决引用；项目关系中心直接编辑仍独立延期，不阻塞S1。静态对拍不替代实际接口、事务、PDP、浏览器测试或S0 CI退出门槛。

### 12.4 下一阶段明确文件归属与验证

前端由本代理统一负责`api/authz.ts`、`api/accounts.ts`、`authz/*`、`pages/accounts/*`以及`nav.tsx/App.tsx/AppShell.tsx`共享接缝。先接通真实销售的公共路径、保留其余域显式状态，再逐域迁移；不复制SalesAuthorizationDrawer形成85份表单。业务域代理只在交接后改所属页面/组件并消费公共能力接口，不同时改上述共享文件。

根代理独占`app/authz/catalog.py/engine.py/service.py`及principal/runtime、认证/运行态接缝；后端控制面负责人负责新`app/authz/management.py`、`api/authorization.py`、`api/accounts.py`、`api/role_templates.py`和模型/单一迁移，交付一个规范写服务及DTO fixture。独立审查负责人覆盖旧入口负例、跨账号迟到响应、模板保存不追改账号、bulk冲突全失败、客户关系原子性与混合账号字段范围；根代理汇总精确SHA和CI后决定阶段推进。

S1前端有意义回归至少包括：默认兼容模式保持；统一模式旧bool账号/模板/bulk写被明确拒绝、规范操作确实改变返回模块包；普通account_manage不能操作新授权；新建账号默认无Grant、分类保存不套模板；显式应用模板不复制关系；保存模板不改账号；保留/清除整资源调整可见；scope/fields差异展示；409指纹失效；跨登录迟到成功/401不能污染；未迁资源只读不被标成已生效；暂停及配置冲突不给legacy fallback；多资源快照无逐资源N+1。仅目录结构和类型检查通过不能替代这些反例，也不能跳过S0 CI门槛。

### 12.5 与后端wire负责人对拍的消费要求

具体路由、DTO字段名及golden fixture由后端wire负责人统一冻结，以根`full-migration-contract`为权威；本节不创建第二套竞争schema。以下消费语义已经与wire核对：

- 账号模块读取保留显式个人覆盖的**存在性**和模板快照配置。覆盖恰好等于模板仍是覆盖，`actions=[]`仍是显式拒绝。`clear_override`恢复账号已保存快照；追随最新模板使用单独同步预览。仅来源变化也推进CAS版本。
- 单账号显式模板应用与bulk共用规范命令/预览形状，preview返回准确目标集合、完整资源before/after、来源和覆盖变化、范围/字段差异及会话影响；不只返回`changed_keys`。执行返回新epoch/资源版本和`actor_session_revoked`；来源变化也推进epoch，不另设source_revision。单账号PUT返回完整result，不要求批量预览的effective/source变化标记。
- 建号默认无Grant；显式选择模板与建号原子提交。模板create/copy及metadata+模块包一次提交，返回完整模板版本；账号同步是其后的独立预览/执行，不在客户端自动串多次Grant PUT。
- 错误保留`detail.message`兼容展示，并有稳定`code`。409说明目标/资源版本冲突且使preview失效，422定位字段；混合旧bool payload整体拒绝，不能剥掉字段后返回成功。客户端不自动重放冲突写入。
- 其他账号密码重置由服务端实时授权管理员能力决定；本人改密保持独立流程。对象capability绑定领域`authorization_revision`，不把`project.version`误认成所有项目关系版本。
- 当前销售快照旧顶层字段由同一结果派生；unified只读resources，缺新schema不能自动回落旧允许。客户和账号ID保持safe integer number。Provider保持批量单请求，bulk保持单个服务器事务。
- 普通bulk无秘密可回放服务端command；管理员建号预览不含password，执行才由本地补入，响应/审计不回显秘密。管理员重置密码使用直接epoch CAS，不能套普通预览回显协议。

请求归属同时绑定操作者token、目标、sequence和协议版本。任何模板、清除覆盖选项、目标集合、资源或关系草稿变化都废弃旧preview；关闭窗口也使迟到结果失效。账号目标诊断不进入操作者Provider。JSON中的`control_plane_ui_draft.wire_consumer_requirements`仅记录消费要求，具体shape已被唯一wire产物替代。

最终静态检查覆盖共同envelope、闭合集合/依赖、equal/empty override存在性、版本与epoch、客户ID标签、planned/retired空配置、暂停deny、销售旧顶层一致性、预览diff及执行command匹配。两处对拍不一致已修正：客户归属说明明确与配置原子保存；单写完整result与bulk preview变化标记分开。对应fixture校验哈希记于JSON `control_plane_ui_draft.wire_fixture_verification`，仅证明文档样例一致，不证明运行实现。

## 13. S2商业与基础域的挂载/请求清单（计划，未实现）

静态复核基线为 `16c38c3063c2f971d3c0cce65f02924f1250e053`。189个语义面中，本节覆盖72个现行商业/基础面、23资源；不包含同属B30索引的S1账号两面和S3维保匹配归因面。没有增加运行许可或把计划动作标为S1可执行。下表路径省略`frontend/src/`；surface写作`资源.{后缀}`，展示动作为机器草案登记的对应`show_*`。操作面列出的多个写动作逐按钮裁决，不要求全部写权限才能显示只读内容。

### 13.1 销售共享数据请求：八个S2动作，十二个展示面

| 真实挂载/调用点 | 展示面 | S2数据动作与接入要求 |
|---|---|---|
| `sales/analytics/SalesAnalyticsPage.tsx:194`与`FilterBar.tsx:115`（路径前缀`pages/`） | 共用筛选器，无新增独立surface | `read_options`；父初始化与客户debounce都须检查；客户候选仍受字段/客户范围约束 |
| `SalesAnalyticsPage.tsx:418`挂`SummaryView`；父effect在`:215`请求summary | `sales_analysis.{summary_counts,summary_amounts,summary_profit}` | `read_summary`；至少一个需要汇总的合法消费者才请求；逐卡show分别控制挂载。营收revenue仍可公开，不能把整个amounts面板强制依赖cost |
| `SalesAnalyticsPage.tsx:450/458/467`挂`OverviewTab/CompactTab/GroupsTab`；请求在各组件`:40/:84/:124` | `sales_analysis.{overview,compact,groups}` | 共享`read_groups`。概览另在`OverviewTab:107`使用`read_trend`；不以`show_groups`控制概览的分组请求 |
| `SalesAnalyticsPage.tsx:476`挂`TrendTab`；`:77`请求trend | `sales_analysis.trend` | `read_trend`，与概览共享；禁止用tab参数作为后端授权依据 |
| `SalesAnalyticsPage.tsx:328/482`挂`DrillTable`；父`:245`请求orders | `sales_analysis.orders` | `read_orders`；当前仅有tab=orders的请求条件，S2再加明确许可；URL、图点击、返回导航都不能挂未授权tab |
| `SalesAnalyticsPage.tsx:394`挂`AmountHistogram`；组件`:39`请求 | `sales_analysis.amount_histogram` | `read_amount_histogram`与cost字段；三种金额口径都保持原403规则。`AmountRangeFilter`是输入控件，不是直方图请求源 |
| `SalesAnalyticsPage.tsx:487`后挂`DetailDrawer`；抽屉`:101`请求同一详情 | `sales_analysis.{order_detail,reconciliation}` | 共享`read_order_detail`；订单与对账两个show分开。对账字段仍需reconciliation+cost；隐藏对账不撤销普通详情数据能力。无detail许可时URL orderId不能触发请求 |
| `SalesAnalyticsPage.tsx:437`导出按钮、`onExport`调用`downloadSalesOrdersCsv` | `sales_analysis.export` | `export`及`read_orders`；导出按钮show独立；CSV使用同scope/fields/filter/sort，不能由show或兼容read放行 |

S1执行目录继续只有`read/export`。S2可保留旧read为兼容说明，但上述八种请求逐项使用具体数据动作；任何一个缺失都不能用旧read补allow。展示门应在子组件挂载前，父级effects、远程候选、重试、弹窗深链、导出handler各有请求许可；仅返回null或隐藏Tabs label不足以停止请求。许可/epoch变化需清空旧响应和请求代次，保持S0已有token/owner隔离。

### 13.2 其余商业/基础面逐资源接入

| 资源与surface后缀 | 当前挂载与真实请求源 | 数据动作/必须分开的接缝 |
|---|---|---|
| `company_summary.{kpi_sales,kpi_purchase,kpi_profit,kpi_coverage,order_health,profit_ranking}` | `pages/BossBoardPage.tsx:114`→dashboardKpi；`:49`的KpiStrip及父健康文字复用响应；`pages/boss/RankingBlock.tsx:77`→dashboardPartRanking | `read`；KPI五面与排行show分别挂接；财务字段/排序不能因另一张卡隐藏而改变后端数据资格 |
| `company_sales_orders.recent_orders`、`company_purchase_orders.recent_orders` | BossBoard挂两个`OrdersBlock`；`pages/boss/OrdersBlock.tsx:118/119`分别dashboardPurchaseOrders/dashboardSales；`:295`PartsTable复用行内parts | 各自`read`；销售旧scoped整体禁止逐单，不能借采购块或公司汇总授权。parts不再请求但仍是敏感返回块 |
| `company_pool_analysis.pools` | `pages/boss/PoolsBlock.tsx:88`与`FilterBar.tsx:51`均调用dashboardPools | `read_summary`；隐藏PoolsBlock不影响有资格的筛选消费者，但无read_summary时筛选器也不得请求。旧dashboardPool详情API当前无组件caller，`read_sales_transactions/read_customer_cross_brand`保留API-only，不伪造面板 |
| `price_discipline.summary`、`company_pool_order_facts.order_detail` | `pages/boss/MorningDisciplineSummary.tsx:147/271`→summary并挂共用Modal，注入fetchDashboardOrderDetail；`components/pools/PoolOrderDetailModal.tsx:45`调用loadDetail | 两资源各自`read`。同Modal默认loader属于pool_order_facts，注入loader属于company合同，入口资格不能OR；治理价格字段独立于cost |
| `part_lookup.{search,purchase_history,sales_history,inventory,quick_pricing}` | `pages/PartSearchPage.tsx:115/185`分类与fetchOverview；搜索调用unifiedSearch；`:449`后摘要、`:516`库存、`:557`交易Tabs均复用ov | 分别`read/purchase_records_read/sales_records_read/inventory_read/financial_summary_read`；quick_pricing的询价/市场块还需`market_prices_read`。一次overview响应须在后端按块裁决，不能因search.read拿到整包再由UI隐藏 |
| `part_substitute.{relations,manage}` | PartSearch的ov.substitutes；`:263/275`POST/DELETE substitutes | `read/manage`；读取与写入分开，关联库存摘要不得绕过其数据合同；改关系不隐式改人工池 |
| `part_master.{inline_edit,list,create,edit,normalize}` | `pages/MasterDataPage.tsx:75/87/146/178/202/220/238`分类、搜索、编辑基线、suggest、check、新建、保存；`:473`挂BatchNormalizeModal；`components/parts/InlinePartEditModal.tsx:98/105/112`另有overview/edit-state/categories；`BatchNormalizeModal.tsx:86/100`预览/应用 | `read/create/edit/normalize_preview/normalize_apply`。inline编辑必须覆盖其额外overview查询，不能借master.edit读取整份交易全景。主数据列表搜索复用part_lookup身份合同；suggest/check/edit-state各随真实操作资格，不仅保护保存按钮 |
| `profit_analysis.{summary,export,recompute}` | `pages/ProfitPage.tsx:114/144/133`→GET profit、export、POST recompute | 按归一dimension选择`read_part_analysis/read_salesperson_analysis/read_customer_analysis`；导出另需export；recompute独立。scoped旧账号只授part，不把all范围误换成assigned_customers |
| `purchase_analysis.{summary,groups,trend,orders,order_detail}` | `pages/purchases/analytics/PurchaseAnalyticsPage.tsx:178/219/247/279`→options、summary分组、summary时间桶、orders；`:447/464/487`挂SummaryView/DrillTable/OrderDetailDrawer，后者`:101`请求详情 | `read`，旧新采购页额外admin/boss且非scoped门保留为seed谓词。summary/groups共用响应，trend使用同summary接口不同参数；五面show不能变成接口tab门，客户维度须SQL前检查 |
| `purchase_records.{model_analysis,price_trend_advice,exceptions,records}` | `pages/purchases/PurchaseAnalysisPage.tsx:189/48`分析/逐笔drill；`PurchaseExceptionsPage.tsx:44`取消统计；`PurchaseRecordsPage.tsx:59`recent列表 | `read`；建议与分析共用结果。桌面展开与手机抽屉另挂PoolReferencePanel，必须独立校验pool_price_analysis，不从purchase读自动给予池价格 |
| `inventory.{list,adjust}` | `pages/InventoryPage.tsx:65/105/119`dynamic、手改、撤销覆盖 | `read/adjust`；adjust旧admin资格单独迁移，撤销覆盖与修改同数据动作，保留reason/版本语义 |
| `pool_price_analysis.{part_reference,list,members,price_map}` | `components/pools/PoolReferencePanel.tsx:39`part reference，调用方含型号/采购多页；`pages/PoolsPage.tsx:102`列表；`PoolAnalysisPage.tsx:167/211`详情/price-map，挂PoolPnPriceMap | `read`；price-map请求由父发起，不能只包住子图。part-reference不是父路由授予；隐藏一个引用面不撤销其他合法引用。价格、价差、质量数及排序均受governance字段，不能改用cost |
| `pool_order_facts.{purchase_facts,sales_facts,order_detail}` | `pages/PoolAnalysisPage.tsx:167`同详情响应包含两侧订单；`:636/652`表；`:724`共用Modal，默认fetchPoolAnalysisOrderDetail | `read`。父pool_price_analysis详情与pool_order_facts两合同逐块查询/返回；没有事实read时不可返回订单行/total，即使成员页显示允许 |
| `pool_catalog.{list,identity_edit,members_edit,lifecycle}` | `pages/PoolManagementPage.tsx:110/265`list/detail，`:422/453/505/532/533`edit/members/create/archive/restore | `read/create/edit/edit_members/archive/restore`；身份与成员可读不自动包含政策。父表单/预加载/get详情也须逐块授权，归档状态规则保留 |
| `pool_price_policy.{read,set}` | PoolManagement`:820`挂PoolPolicyCoverage；`:479`setPnPoolPolicy；另`pages/boss/PoolsBlock.tsx:96`用listPnPools取coverage | `read/set`；共享list/detail中的政策、history、coverage、policy_missing都须合同检查。set依赖governance字段；保留keep/set/unset和CAS，不能看到池名就获取政策 |
| `governance.{parts,issues,metrics}` | `pages/GovernancePage.tsx:83/91/97`parts/exclude；`:238/246`issues/resolve；`:461/462/469/478/491`top summary/metrics/recompute/refresh/classify | 资源`read/exclude/review/refresh/classify`；PartsTab`:102`confirm-batch另属`part_identity.confirm`，top重算另属`profit_analysis.recompute`。Tabs切换前过滤合法项；顶层请求不能等tab挂载才检查 |
| `part_identity.{candidates,merges,aliases}` | Governance`:166/176`candidates/review；`:354/363`merges/unmerge；`:294/303`aliases/review | 候选surface保留稳定ID，实际data_requirements为`governance.read/review`（payload merge意图）；merges用`part_identity.read/unmerge`；aliases用read/review_alias。直接merge/rename API无当前UI caller，保留API-only动作，不造按钮 |
| `data_quality.issues`、`data_quality_calibration.preview` | Governance`:538/539`挂两Panel；IssuesPanel`:120/151/166/186`list/detail/refresh/review-reopen；CalibrationPanel`:80`预览 | `read_purchase_issues/read_sales_issues`按请求或对象side裁决，review同时需对象side读与cost；未指定side列表也限制SQL/total。calibration.preview独立需cost，不额外要求review |
| `display_policy.{read,update}` | `context/TaxBasis.tsx:135`全局getSystemSettings；`pages/SystemSettingsPage.tsx:76/96`GET/PUT | `read/update`；全局税口径读取不能以设置页show_read当门。`read_audit_identity`单独控制GET与PUT中的updated_by/at；旧admin来源只用于迁移，运行不能永久读role |

附带搜索消费者：`components/PartPicker.tsx:49`统一unifiedSearch供经营看板等页面使用，必须依实际身份搜索数据能力；不能把`show_search`（型号查询页展示）作为所有PartPicker的后端门。上述复合响应允许按合同省略/限制块；不能把data_requirements数组误解释为“所有块都允许才返回任何内容”。

### 13.3 显式迁移与必要反例

销售已cutover，升级输入只取canonical Grant及保存来源。旧read映射六项基本读；原read且cost可见才补read_amount_histogram；原export保持并配套read_orders。旧可见的常规页签、summary_counts/summary_amounts与详情显式补对应show；summary_profit需profit+cost、reconciliation需reconciliation+cost、histogram需cost、show_export需原export。空动作/缺Grant不授，scope、fields、客户集合不扩大；不把新面板或新能力预先授出。

同一版本转换必须处理模板canonical bundle、账号保存模板快照、完整override和materialized Grant，保留等值override存在性、空动作拒绝与旧快照不追最新模板的语义。来源与Grant按账号原子转换，相应version/epoch/catalog推进，旧preview失效；重复升级no-op，不能重加管理员后来撤销的动作。S2动作目录启用以前先完成全部输入转换与差分验证；未转换状态失败关闭，UI绝不做read→show或read→细分read fallback。

按[独审S2无扩权矩阵](s2-seed-no-expansion-review.md)至少补：仅read_groups可查分组但不可查订单/趋势；关show_groups而保留概览与read_groups时概览继续工作；关read_groups时所有分组消费者停请求；深链未授详情不请求；cost=false无直方图请求但营收卡仍可读；候选debounce与迟到响应跨epoch不污染；公司池scoped只summary；利润scoped只part且CSV同门；疑点省略side/直接sales ID/reopen均不能旁路；设置普通read不返回审计身份。静态索引完成不等于这些运行反例已通过。

S1目录action新增必填`requires_fields`，与requires_actions同样闭集校验，Editor展示缺失项但不自动授字段。S0read/export依赖数组为空；以上S2字段依赖和数据动作仍为planned。具体wire已由后端与独审确认，未因本清单提前实施业务迁移。

### 13.4 S1冻结后的实施分工差异（仅准备，未改应用/测试）

13.1–13.3已足够作为72面清单；不再重做目录。按实际共享文件收成一个公共包、三个业务包，计数互斥相加为23资源/72面。下表路径均相对`frontend/src/`：

| 包 / 顺序 | 资源、面数 | 独占文件与共享接缝 |
|---|---|---|
| F0 公共接缝先行，并接全局税口径 | display_policy：1资源/2面 | `api/authz.ts`、`authz/AuthzProvider.tsx`、`authz/events.ts`、新增单一授权读取请求hook、`App.tsx`/`nav.tsx`/`AppShell.tsx`、公共`api.ts`、`api/systemSettings.ts`、`context/TaxBasis.tsx`、`pages/SystemSettingsPage.tsx`。其他包不改这些文件，向F0提交所需DTO/路由绑定。普通业务用户不得调用管理员catalog来渲染页面。 |
| F1 交易与财务 | sales_analysis、purchase_analysis、purchase_records、profit_analysis、inventory：5资源/26面 | `pages/sales/analytics/*`、`pages/purchases/*`、`pages/ProfitPage.tsx`、`pages/InventoryPage.tsx`、`api/salesAnalytics.ts`、`api/purchaseAnalytics.ts`。统一接F0请求上下文；Profit按dimension选具名动作；sales存量动作显式转换完成后才开show。 |
| F2 经营看板与池 | company_summary、company_sales_orders、company_purchase_orders、company_pool_analysis、price_discipline、company_pool_order_facts、pool_price_analysis、pool_order_facts、pool_catalog、pool_price_policy：10资源/24面 | `pages/BossBoardPage.tsx`、`pages/boss/*`、`pages/PoolsPage.tsx`、`pages/PoolAnalysisPage.tsx`、`pages/PoolManagementPage.tsx`、`components/pools/*`、`api/poolAnalysis.ts`、`api/pools.ts`。此包独占共用池详情Modal/引用组件；调用方显式传resource合同，不能默认loader与注入loader资格OR。 |
| F3 型号、主数据与治理 | part_lookup、part_master、part_substitute、governance、part_identity、data_quality、data_quality_calibration：7资源/20面 | `pages/PartSearchPage.tsx`、`pages/MasterDataPage.tsx`、`pages/GovernancePage.tsx`、`components/parts/*`、`components/PartPicker.tsx`、`pages/governance/DataQualityIssuesPanel.tsx`/`DataQualityCalibrationPanel.tsx`、`api/search.ts`、`api/dataQuality.ts`。共享搜索只检查数据动作，不继承型号页show；overview逐块投影，inline edit不附送交易全景。 |

F0统一提供纯读取`actionAllowed(resource, action)`、资源内`fieldAllowed(resource, field)`和展示面绑定读取；Boundary只负责是否挂载，数据hook独立接`requirements + queryKey + loader(capturedToken, signal)`。业务包不复制Provider、不推导role、不调用`/check`为每行做N+1。页面入口取“至少一个该页真实可用surface”的OR，单个surface再按自身display及必要data组合；不能把所有数据块扁平AND成整页门，也不能把兼容`read`当S2销售总开关。未知、缺项、suspended、requires_context均不当allow；未迁资源仅在服务器明确legacy状态走旧路径。跨页池引用、PartPicker和TaxBasis是独立消费者。

代码核对新增两处必修接缝：`sales/analytics/useAsyncData.ts`当前`enabled=false`直接return，既不清旧数据也不作废在途响应；`pages/boss/shared.tsx:useGuardedFetch`只有查询scope/代次，没有授权enabled或epoch。S2不直接把show布尔塞进去就宣称安全：公共hook必须在权限失效首帧屏蔽旧结果、递增代次/abort，成功/失败落地都校验捕获token与snapshot.version、canonical queryKey；retry、debounce、export、点击后刷新也使用同一捕获上下文。旧两个hook可作薄适配，不各自维护第三份权限真值。

响应/快照仅有以下差异需要后端对拍；后端代理确认尚无新的S2响应DTO冻结，以下不冒充已存在字段：

- `/me`继续S1的`resources[id].decisions[action]`、`field_permissions`、`scope_kind`、epoch/catalog/version，不引入第二套全局canSeeCost。S1前端validator及`resourceAllowed`目前硬编码sales/read/export；F0需改成S2闭合词表，同时校验未知action/field失败关闭。展示动作逐项显式输出，缺show绝不从read补；`/catalog`planned元数据与运行执行词表仍分开。
- **必须可表达复合块拒绝，而不是只有空数组**：`Overview`目前仅`sales_recent_restricted`不足以表达采购/库存/财务/市场/互替分别拒绝；`PoolAnalysisDetail`需表达成员价格合同与逐单事实分别拒绝；公司池详情需表达summary/sales_orders/customer_cross_brand分别拒绝；池管理list/detail需表达policy/history/coverage拒绝。优先复用已有明确restricted结构；缺处建议统一`block_access`（固定块ID→S1 `Decision`形状）而非每端点自造标志，字段名由根代理冻结。它只解释本次已裁决投影，不能变成新Grant或display门。拒绝块不运行查询，行=[]、total/财务汇总=null，不把0当无资格；实际空结果仍是allow+空结果。
- 对返回`requires_context`的动作，现有对象读取响应须携带绑定稳定对象ID/对象version的`capabilities`及当前主体epoch/catalog；没有对象allow就不启用写按钮。不新增通用任意对象check，也不逐行请求。S2均可直接全局裁决的动作不强制添加无用对象DTO；项目关系版号属于S3再冻结，不能凭账号epoch替代。
- `display_policy` GET和PUT都按`read_audit_identity`省略updated_by/updated_at；既有optional类型足够，无需新响应字段。TaxBasis读取依赖read，不依赖设置页show；拒绝时停止刷新/广播重取并保持口径不可用，不能用默认口径伪装已读成功。数据疑点现有side/对象version/restricted可复用，但省略side列表、total及直接ID必须先由后端按真实side裁决。

分工可直接按F0→F1/F2/F3并行推进；公共`api.ts`仍单owner，域包只提交该文件的DTO/签名需求。展示门在父挂载/合法tab/深链层，数据门在共享请求hook与每个handler，后端再次以具名动作验权并按块查询；三者都要覆盖，show不进入后端查询授权。S1当前app/tests保持冻结。

### S2 实施补充：共享金额展示口径

`display_policy.read` 是解释全局税口径金额的独立展示依赖，不随任何业务 Grant 自动授予；编辑器明确提示其目录名称“全局展示与业务默认值（金额展示设置）”。缺权时显示“缺少金额展示设置的读取权限”，不伪装成网络故障，也不把 DEFAULT 当作已读取结果。`nav.requiresTaxPolicy` 只标注展示依赖，不决定业务 API 授权；其中命名谓词复用生成的 surface Rule 与同一主体字段快照。

- 整页依赖：互通池、利润分析、维保数据看板、池分析详情。
- 依据实际金额授权动态依赖：经营看板按可见面/本资源字段判断，health-only 数量可用、营收单元自己失败关闭；采购型号分析/异常/明细仅本域成本字段可见时要求整页口径。无成本时金额列/单元隐藏，数量保留。
- 局部依赖：型号查询和库存保留身份/数量/关系，金额列与金额块按本域字段及 taxReady 拒显；数据治理仅 PartsTab 营收列受约束，其余队列与身份能力独立；跨页 PoolReferencePanel 独立边界，池逐单详情只在金额单元拒显。主数据批量规范化的 TaxMoney 单元自己失败关闭。
- 不依赖：账号与权限中心、系统设置，以及其他没有全局口径消费者的路由。销售分析与采购分析使用本地/API 明确金额口径，池管理使用输入价自带税基，不额外绑定 display_policy.read。

policy owner 变化时第一帧同步不可用，不等待 effect；整页边界会卸载 AntD portal，未用 display:none 冒充撤权。全局 Router 仍只按 token 重挂，已迁页面按自己的授权 generation/version 清理。真实 App/Shell/Provider 测试覆盖共享 read 缺失下的控制面、数量块、字段两态、局部金额拒显；独立 layout-effect 测试证明新授权快照第一帧不暴露 DEFAULT。最终实施与验证见 [S2 前端审查](s2-frontend-implementation-review.md)。
