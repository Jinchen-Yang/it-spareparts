# 后端维保架构复核（独立审查）

基线：`7a9fd20` 及当前未提交候选。范围：枚举全部 `services/maintenance*.py` 与 `api/maintenance*.py`，对 AST import、函数、跨模块调用、提交点建立清单；逐段核对权限、有效需求读取、双成本算法、工作簿、原件差集、现场领用、返还收货及标准导入。**不是全部 8 万余行逐行正确性审计**。当前未连接生产、未提交、未部署。下述初审发现的WorkbookError反向依赖，已在根授权追加的小批次中解决；最终验证见文末。

## 判断

候选 scope/read/audit 抽取是真实改善：原来 row_workbench 依赖 API 权限及 7509 行总表模块，现在从中立服务取得范围与有效明细；原件同步的通用追加审计也不再调用总表私有函数。原函数迁移未改变SQL、事务、错误或授权语义。然而直接 import 消失不等于完整领域解耦；下列循环仍需真实 seam 逐步拆解，不能宣称新增维保功能已无需改核心。

## 职责与扩展主干

| 模块组 | 权威职责 / 输入事实 | 下游 | 扩展限制 |
|---|---|---|---|
| project_identity / catalog / project_assignments / source_assignments | 稳定项目、XSDD身份、主负责人/销售/查看者、来源归属 | 所有查询和命令 | 名称不代身份；项目直达与目录范围不同；变更归属需要版本与锁 |
| demands / assigned_lines / query_filters | 墓碑、行级软作废、来源状态构成有效性 | 行表、成本、导出、看板 | tombstone遵守cutover；来源非生效独立过滤，不能擅自统一 |
| row_filter_registry / row_workbench | 类型化字段、权限前置、SQL筛选/count/完整导出 | HTTP查询、CSV/XLSX核对导出 | 不可伪造可回传身份；金额筛选与派生枚举也是敏感数据 |
| cost / cost_reference / margin | WBDD需求出库成本及合同利润口径 | 历史成本查询、导出、Agent | 与现场消耗成本不相同；未定收入分摊不能变单项目毛利 |
| consumption_cost / cost_invalidation | 现场领用成本证据与更新 | operations、现场编辑、总表 | 当前已有需求价格证据优先层，不能仅凭早期ADR三层瀑布重写 |
| operations / manual_site_issue / site_issue_facts | 领用命令、锁前缀/版本/回执；既有事实编辑原语 | 页面、V1/V2总表、原件级联 | 原语不提交、不拿上层锁、不接管幂等及revision；新建/作废是外层命令 |
| site_return_requirements / bad_returns | 领用义务事件与旧native obligation投影 | 现场维护、总表、原件级联 | 现场事实、义务、实收与库存不能合并为一个状态机 |
| return_receipts / return_request_summary | 实收事实与需求N/R容量 | 页面登记/编辑、标准导入、看板 | 手工/导入同台账；同需求串行；历史超额保留，减量/作废可行 |
| master_workbook / expense_collection_workbook / roundtrip / adapters | 签名、基线、3路合并、整本事务与首次回执 | Excel API | 每sheet独立提交会破坏整本原子性；旧版协议不能草率替换 |
| wbdd_source_snapshot / reconciliation / wbdd_sync / source_cancel_cascade | 原件不可变成员、缺项/终态影响预览、实名确认 | WBDD导入与同步API | 坏字段不等于源行缺失；只精确级联，不按PN猜测；不动实际实收 |
| doc_import / return_receipt_import / ckd_import / ledger / bulk_import | 各协议原始材料、排队/解析/预览/apply | 所有事实源 | 多阶段commit存在任务状态用途，不代表业务原子性失效；勿统一成通用导入器 |
| boss_board / analytics / explorer / dashboard_* | 当前授权范围内事实投影与指标 | 页面/图表/导出 | 数量、估值、已计成本独立；未知不补零；保持单位桶与来源证据 |
| collection_* / expense_* / acceptance_* / warehouse_* / migration_* | 独立回款/报销/验收/仓库/迁移子域 | 各工作台与导入适配器 | 有些生产写入口仍关闭；架构整理不能顺便启用 |

完整函数/import/callsite清单：`backend-ast-inventory.json`。含每个文件函数起止行、imports及调用点。

## 事务和锁的调用图

```text
HTTP网页逐行编辑
  -> 外层实名/项目/字段授权
  -> 命令幂等锁 -> workbook state -> project -> issue -> issue lines
  -> apply_locked_site_issue_edits
       -> 校验全部字段 -> 事实赋值 -> 必要时批量取价
       -> record_corrections -> append audit/event -> 旧义务consumer或ack
  -> 外层revision + 原回执 + commit

总表V2整本apply（单Session）
  -> DATA_CHANGE排他advisory
  -> expire_all（清验证期identity-map）
  -> XSDD identities -> sorted workbook states -> project
  -> operation identity -> operation row / replay
  -> source order -> source assignment -> changed facts / row guards
  -> 03事实/成本、04/05 ec.apply(commit=False)、06原语/合单
  -> maintenance_cost.recompute(commit=False)
  -> revision一次 -> operation + first receipt snapshot -> commit

原件缺项确认
  -> DATA_CHANGE排他 -> batch -> candidates digest
  -> sorted states -> source orders -> assignments -> demand lines
  -> issue heads -> issue lines -> 重算candidate digest
  -> 精确级联 -> corrections -> void_fast复用锁集
  -> revision -> terminal digest + replay result -> 调用方commit

返还登记/更正
  -> DATA_CHANGE共享 -> shared project(s) -> existing receipt(s)
  -> sorted return-capacity:{source_order_id} advisory
  -> 锁后权威N/R汇总 -> 容量检查 -> fact + audit -> 调用方commit
标准返还导入
  -> context shared -> import-apply identity -> project name identities
  -> workbook states -> project(s) -> batch -> existing receipt rows
  -> sorted demand capacity locks -> 锁后整份plan + capacity
  -> 按终态先抵减再增加 -> 任一invalid整份拒绝 -> facts/audits/batch -> commit
```

证据：`operations.py:321-387`、`site_issue_facts.py:202-350`、`master_workbook.py:6318-6540,7246-7258,7400-7444`、`wbdd_sync.py:49-216`、`return_receipts.py:108-165,234-260,389-505,653-825`、`return_receipt_import.py:373-437,966-1152`。文件均位于 `backend/app/services/`，行号为额外错误模块抽取前候选。

这里最大扩展风险是锁协议依赖各写者自律。`master_workbook.py:5470-5499` 已清楚记载05快照权威性依赖五个不同写者至少持有global/state/project一把锁；新增写者若绕过三者，本体仍能运行却失去并发保护。下一轮应建立“所有快照写者”行为契约，不能只在注释写规则。

## 四组循环与真实成因

第一版候选的service-only AST（含函数内惰性import）发现四组SCC；追加错误模块抽取后第一组由10个节点变9个，其他组保留：

1. 10模块：`consumption_cost, expense_collection_workbook, site_return_requirements, bad_returns, project, project_identity, project_catalog, project_assignments, project_operations, demands`。核心混合了范围、状态锁/版本、读模型、写命令、表示层错误。根完整图纳入非maintenance依赖后另有更大的相邻组，统计口径须标注。
2. `doc_import ↔ return_receipt_import`：RKD在既有材料导入注册表/锁和新收货流水间互相适配；不宜通过新增统一基类掩盖。
3. `manual_site_issue ↔ site_issue_facts`：原语为校验需求归属反向调用 manual adapter 私有 `_resolve_demand_project`（facts:191-199）；应抽出“需求归属校验”中立能力，让二者同向依赖。
4. `collection_evidence ↔ collection_reminders`：附件证据与提醒状态业务互调；须先确认同事务变更，再抽公共命令。

SCC不是证明运行死循环/死锁；惰性import只是推迟加载，不消除变化传播。120个跨维保service私有调用点是维护线索，不直接当成120个bug。

## 风险假设及交叉证伪

| 假设 | 复核结果 | 等级与下一步 |
|---|---|---|
| 抽出assigned_lines后空集合可能变全量 | 已证伪：`assigned_lines.py:68-72`用 `is not None`，空集合生成SQL空集；旧函数SQL完整迁移，直达/目录差异在row_workbench:71-94保留 | 无本轮回归发现；扩展测试应含None/empty/direct矩阵 |
| 独立04/05提交会破坏V2整本事务 | 当前调用已证伪：master:7246-7255显式commit=False、bump_revision=False；cost重算:6930-6933同样commit=False；最终7443统一commit | 未来误用风险中；新命令抽取应默认caller transaction，兼容顶层API显式commit |
| 标准返还导入绕过网页容量限制 | 已证伪：import:373-437共用ledger.check_receipt_capacity，locked=True先取同需求advisory，终态先扣减再增加，blocking_errors阻断整份apply | 未执行新的并发压测；既有DB测试应作为迁移硬门 |
| 原件同步审计移出总表即完全脱离工作簿 | 初审未成立：sync→site_return_requirements:235-246只为WorkbookError引用expense_collection_workbook；本轮追加抽取后此反向边已消除 | 架构P2，本轮追加中立错误模块可移除这条反向边，保持旧类型别名 |
| ExpenseTab未隐藏金额列导致泄漏 | 后端证伪：api/master_workbook:347依赖profit门，api/expense_collection_workbook:96-111在查询前403 | 可能是前端能力显示与接口不一致；不能报告成金额泄漏 |
| can_edit_master_workbook与data_profit可合并 | 证伪：primary_manager享D-03项目特例；销售仍要data_profit，目录/采购成本又不同 | 必须命名不同capability；不能一键canViewMoney |
| AcceptanceTab迟到预检有跨权限写入 | 与前端agent交叉：后端api/acceptance_checklist:197-201按batch.project_id重新scope+owner；双方都有权时仍可能误改旧项目 | 前端已反例复现；是项目上下文误操作，不是已证实越权 |

## 有优先级的扩展路线

**批次0，本轮限定的结构改善。** 保留scope/read/audit/Agent catalog；追加中立WorkbookError定义和旧入口重导出，并让领用义务/合并引用中立错误。保护类型identity、payload、HTTP映射与实际冲突回滚。不同协议异常不贸然合并。

**批次1，公共合同参考读取（P2）。** `boss_board._card_contracts`被cost:1245、master:347、v3:400、manager adapter:539消费；它包含当前生效合同、跨项目冲突、销售批次fallback、未知金额，不是简单SUM。抽 `contract_reference` 带稳定输入输出，继续使用“参考”语义，不把它和完整经营事实/历史as_of读合并。测试共享合同、未映射、缺额、金额权与fallback。

**批次2，领用事实端口（P2）。** 抽需求归属校验和事实快照表示，解除facts→manual与facts→operations；原语保持不commit、不加上层锁、不分配receipt。命令层维护profile兼容，原流程原样回传、PN更正成本失效、qty回算与义务事件数必须不漂移。原语interface的前置锁条件应可测。

**批次3，事务协调与workbook sheet扩展（P1变更风险，非现存事故）。** 使用计划对象集中收集需要锁的身份集合，协调器统一排序上锁/新鲜读/授权复核/commit，sheet策略只提供parse/plan/locked apply。不让各策略拿晚锁或自行commit；保留三路合并、删行作废、签名、完整回执、legacy无快照分支。先对6张sheet组合+故障注入建特征测试，再搬代码。

**批次4，导入共享工具与业务适配。** 仅抽重复的安全上传、不可变原件归档、任务状态、稳定身份规范化；WBDD、CKD、RKD、账本的事实语义保留独立。原件差集、终态确认、Excel删行是三套协议，不能靠统一import mode掩盖。

**批次5，架构约束。** 当前direct AST规则能防服务→API重现，却不能说明transitive decoupling。建立允许依赖图、写者清单与SCC预算；失败需给最短反向路径。避免硬性“service文件不得含workbook字符串”，因为兼容错误名称与状态版本并不等价于Excel适配器。

## 剩余不确定性

- 未对全部 API逐端点执行权限矩阵，也未做生产负载/锁等待/真实数据分布分析；没有生产缺陷或性能结论。
- AST包含动态路径但不解析运行期函数对象、反射和外部调用，边可达不等于热路径执行。
- 早期ADR与当前消费成本实现差异需按后续decisions核对；不能因为发现不同就自行统一算法。
- 原有500余回归由根运行，本子任务避免重复；下文仅记录本子任务追加验证。


## 追加实施与最终证据

按根明确追加授权，只实施了这条已定位的错误协议依赖整改：

- `backend/app/services/maintenance_errors.py`：保留 `WorkbookError` 原类名及 `ValueError`继承、constructor位置/关键字参数、code/message/issues/details及默认值；定义移至不依赖数据库、FastAPI或Excel的中立模块。
- `maintenance_expense_collection_workbook.py`：旧路径重导出**同一个类**，没有子类包裹，没有多套类型；既有except、API错误翻译和所有调用者兼容。
- `maintenance_site_return_requirements.py`、`maintenance_site_issue_merge_references.py`：同向依赖中立错误模块，不再只为错误类型加载Excel应用模块；没有改锁、flush、commit、revision或审计逻辑。
- `test_maintenance_error_contract.py`：旧路径捕获与HTTP payload、fresh-process模块加载边界、真实native稳定发货PN冲突的错误翻译与完整rollback、跨项目合单引用冲突。
- `test_maintenance_module_boundaries.py`：在直接import约束之外，从assigned/access/row/sync四个根检查所有AST可达路径，拒绝HTTP与显式Excel适配器清单；失败给可达路径，包含函数内惰性import。

验证均在 `127.0.0.1:55439` 的pytest独占新建库完成，两进程均exit 0并完成清库：

1. 错误契约、原/新模块边界、native及非native领用义务、合单live references和真实flush回滚定向回归：**27 passed，42 deselected**。日志 `/tmp/maintenance-error-contract-test.log`。
2. 后追加的传递依赖边界单独运行：**1 passed**。日志 `/tmp/maintenance-transitive-boundary-test.log`。
3. AST精确对比：assigned_lines_query、原_write_audit、WorkbookError三处迁移与HEAD基线除了名称/docstring外全部相同。见 `backend-extraction-equivalence.json`。
4. 根完整依赖图脚本重算：`dependency-graph-after-errors.json`。最大SCC从10→9；其余含ETL的5/2/2组仍存在。row_workbench transitive 109→68、wbdd_sync 109→69为基线对最终候选；最终assigned/access/row/sync/agent.tools均不再可达Excel工作簿服务。这是静态依赖证据，不是启动性能/内存或吞吐收益测量。
5. `git diff --check`通过；Agent reviewer独立AST+类型identity交叉检查通过，未发现此抽取回归。

最终判断：本轮建立了可验证的公共查询、项目范围、事务内审计和中立错误契约；剩余领域循环、全模块权限矩阵、所有快照写者统一锁契约仍属于后续批次。不能把这次收口描述为整个维保架构改造完成。
