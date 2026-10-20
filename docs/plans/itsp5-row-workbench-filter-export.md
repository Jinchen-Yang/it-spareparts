# ITSP-5 PR1：行级筛选与完整筛选集导出

决策: D-33

## 范围与基线

2026-10-20，lane B，`feat/row-workbench-filter-export`，起点及开工 fetch 后
`origin/main=32ab2a6f8a82cf075cec3f22282c6888e9f18526`。只实现 D-33 第 1、4、5、6
款；第 2、3 款网页编辑留给 ITSP-5b / #425 之后。不新增迁移、依赖、框架，
不推送、不开 PR、不接触生产。主代理负责后端登记表与工作台，前端子代理单写 frontend，
独立审查代理只读；所有测试由主代理串行调度。

## 现状勘察（基线代码位置）

| 入口 | 集合、筛选、分页及权限 |
| --- | --- |
| 全局行级 XLSX | `backend/app/api/maintenance_project_master_workbook.py:245`，`range/from/to`；强制 `data_profit`，项目范围 `resolve_visible_project_ids`。`services/maintenance_project_master_workbook.py:831` 调 `_assigned_lines`，没有全局网页列表。12 列含项目名、XSDD、单号、日期、PN、描述、需求/退货量、有效来源和两种单位成本、人工原因；另有隐藏身份和签名令牌。 |
| 项目 03 网页 | `api/maintenance_project_master_workbook.py:616`，`sheet/order_no/contract_no/page/page_size`；默认 20、最大 200；目前 `:582` 在 Python 全量取数后筛选分页。`_assigned_lines`（service `:483`）只含有效归属、有效行、有效单头及非墓碑，网页日期倒序不改。V1/V2 行响应 `:642/:742`。 |
| 项目总表 V1/V2 | API `:384`；`sheets` 选择单页/整本。`service:629` V1 03；`:2671` V2 03 字段；`:2713` 生成签名及往返基线；`:3144` V2 整本快照。V2 原始系统成本与网页有效成本不同，不能改名混用。 |
| 旧明细网页/CSV | `api/maintenance.py:194/:586`；`project/month/date_from/date_to/page/page_size`。`services/maintenance_cost.py:1396` 保留 project_std、既有销售范围与成本日期门槛；`:1430/:1576/:1612` count/迭代/分页共用查询。不能替换成项目归属表集合。 |
| 现有金额资格 | 全局 XLSX 的利润门在 `api/maintenance_expense_collection_workbook.py`；项目总表下载 `api/maintenance_project_master_workbook.py:396-412` = data_profit 或 `is_project_workbook_editor`，判定在 `services/maintenance_project_assignments.py:166`（D-03：本人项目负责人可见，销售仍需利润键）。项目 03 网页 `:626` 当前只有利润门，需对齐 Excel 的项目金额资格。旧明细保留 `security.is_field_hidden(ctx, "cost_total")`，不借工作台扩大它的金额权限。 |
| 成本语义 | `services/maintenance_cost_quality.py:178/:223` Python 有效成本；`:520/:546` SQL 人工回退及有效成本表达式；`master:2459` 项目行 API/全局表取同一有效成本。自动证据优先，人工仅对 NULL/none 来源回退。 |
| #381 排序 | `services/maintenance_export_ordering.py:6` 完整键：日期 ASC NULLS LAST → 单号 ASC NULLS LAST → order.id → line_no ASC NULLS LAST → line.id。`tests/test_maintenance_demand_export_ordering.py` 覆盖 V1/V2、逐单、全局、CSV、V3、合同等仍在线出口；保留全部原集合与网页顺序。 |
| 前端全局 | `frontend/src/pages/maintenance/MaintenanceHomePage.tsx:167/:231/:277` 日期与行级表下载，API `frontend/src/api/maintenanceWorkbooks.ts:178`。项目卡墙筛选不冒充行级筛选。 |
| 前端项目 | `frontend/src/pages/maintenance/panel/PartsOrdersTab.tsx:141/:255/:303/:407` 查询、业务列、03 往返下载和分页；`api/maintenanceWorkbooks.ts:294` 行 API；六 sheet 总表 `MaintenanceProjectPanelPage.tsx:474`。 |

## 实施与三路分析结论

1. 新建 `maintenance_row_filter_registry.py`：白名单字段定义、类型、可用运算、
   SQL 表达式及金额信号资格共用。字段并集包括项目/单头业务字段、PN/描述/SN/备注、
   WBDD 数量及流转数量、来源、系统成本、有效成本、人工成本及原因、网页成本证据。
   技术身份、哈希、签名、操作指令和技术池版本不注册。文本 contains/eq/prefix/is_empty；
   枚举 in/is_empty；数值和日期 eq/range/is_empty。多条件 AND，闭区间可单边；
   严格拒绝未知字段、额外属性、非法类型/运算/无效区间，422。
2. 在数据库叠加筛选再 count/offset/limit/流式迭代。将 `_assigned_lines` 的
   SQL 构造提取成可复用 query，原函数保持返回形状、默认顺序及调用者集合。
   旧 project_std 明细只叠加同一白名单谓词，保留自身查询。
3. 新只读工作台入口：全局 `/spare-part-lines/fields`、`/rows`、`/export`；
   项目 `/projects/stable/{id}/master-workbook/row-fields`、既有 `/rows`、
   新 `/rows/export`。`filters` 为 JSON AND 条件数组（field/op/value 或 min/max）。
   全局可显式选 `project_id`；日期参数沿用 range/from/to。
   普通 export 支持 `format=csv|xlsx`，不接收分页，不携带身份/令牌，明确不可回传。
   原全局补价下载、项目 03 / 整本往返下载保留原协议和全集，不将过滤子集放入删行协议。
4. 金额资格在筛选/count 前判定。字段目录仅给当前范围可查询字段；没有金额资格的
   请求直接 403。混合项目没有通用金额资格时，必须先显式选择有权项目，
   不根据金额条件偷偷丢行。无金额条件仍显示全部原有可见事实，逐项目遮蔽金额。
   未实现用户自定义排序，任何提供的排序参数必须验证/拒绝，不能作为侧信道。
5. 前端复用最小筛选构建器；添加/删除/应用/清空；类型化输入和范围；列表、count、
   普通导出共用 appliedFilters，切页不改筛选。全局按需打开只读行表和显式项目选择。
   加载、无结果、失败重试、金额不可用提示；异步响应防竞态，320/390px 实测。

需求/体验分析：用户要跨页核对完整集合，不能只在当前页筛选；原 Excel 编辑习惯保持。
业务/权限分析：D-01 缺行作废使过滤后的往返文件有破坏风险，采用独立核对导出；
D-03 项目金额资格和全局利润资格不可混同。
技术/运行分析：无数据库写入、无迁移、无上传协议改动；新逻辑放新模块，
高风险 master service 只提取查询接线，保留现有排序契约测试。

## 公共 seam 与验收场景

用户已指定公共 API 为 TDD seam；HTTP 返回值、count、CSV/XLSX 实际内容为证据，
真实 PostgreSQL 合成夹具独立列出期望行集。按纵向切片先失败再实现。

| 场景/角色 | 动作与预期 |
| --- | --- |
| 有权用户，多页合成行 | 文本+枚举+数量/日期范围组合；网页 total = 两种完整导出行数 = 已知命中集；第 2 页不重复，导出包含其余页。 |
| 字段类型 | 文本包含/相等/前缀/空，枚举 in/空，数值/日期相等/双边及单边范围/空；0 与空分开；特殊 LIKE 字符按文本处理。 |
| 非法输入 | 技术列、未知列、非法运算、坏日期/非有限数字、倒置范围、未知属性均 422。 |
| 无金额资格/混合项目 | 金额及衍生信号条件 403，无 total/命中数；目录不提供金额字段；未显式限定的混合范围不得自动删行；本人负责人选定项目沿 D-03 可用。 |
| 越项目 | 不可见项目直接 403 或不存在，目录/行/导出一致；现有销售和归属范围不变。 |
| 导出与网页排序 | 使用 #381 同号/同日期/NULL/交错 ID 夹具验证完整升序；网页保持旧顺序；其他导出回归不替换集合。 |
| Excel 协议 | 旧补价/项目往返下载继续可回传；普通筛选文件无技术令牌，不能误走往返删除。 |
| 前端 | 筛选应用与分页/导出参数相同，清空恢复；失败可重试，旧响应不覆盖新范围；320/390px 真浏览器输入、点击、长 PN 和下载。 |

## 验证与交付安排

同一时间一个测试进程，pytest 不开多进程，Vitest `--maxWorkers=2`。开发与最后本地
均只运行新增/修改和相关模块子集；全量交 CI。前端另跑 tsc、vite build。
专用 PostgreSQL 15 容器 `itsp-laneB-rowfilter-20261020`，127.0.0.1 随机端口，
通过 `PYTEST_DATABASE_BASE_URL` 使用；结束只删除该容器。
长命令 nohup，日志在 `~/workspaces/reviews/lanes-20261020/laneB/rowfilter-*.log`。
不提交 egg-info、临时日志；中文 conventional commits 留在当前分支。
完成独立需求及代码审查后报告“待用户点按验收”，不声称已合并/发布。

## 待金臣确认

暂无新增业务口径问题。若实现中发现 D-33 未覆盖的事实语义，记录选项与推荐，
仅暂停该点，其余继续。

## 执行证据

首次 API 红灯：全局新路由 404；旧明细忽略多字段条件（6 行而非 3 行）。
第一轮实现后，全局网页/count/CSV/XLSX 跨页集与数据库独立期望一致。
第二轮 API 26 项通过（`rowfilter-green-api-3.log`）；前端当轮 tsc、25 项组件/
项目 tab 测试、vite build 均通过（`rowfilter-frontend-checks.exit=0 0 0`）。
这不是最终交付验证，后续修复仍须重跑受影响子集。

独立同模型审查补充了三类反例：

- 旧明细 `resolved_line_cost_fields` 与项目行 `_line_cost_evidence` 对历史
  `source=manual`、缺一侧归一金额的投影不同；按出口选择注册表表达式，不修改旧值。
- PostgreSQL `round` 半分远离零，而现有 Python `quantize` 为 HALF_EVEN；
  `qty=.5、人工单位成本=.01` 的金额必须为 `.00`。已补公共 API 失败证据。
- V1 缺成本的来源为空串，必须让 V1 网页、count 与新核对导出一致；XLSX 继续复用
  既有控制字符净化，公式样式文本强制作为文本，超单元格长度明确拒绝。

字段覆盖进一步包含网页互通池名称/状态/是否在池（含并档 PN 与归档优先级），
源流转数量不参与成本净量计算；旧明细兼容原值金额/税口径/层级、数据提示另行命名。
身份/版本/哈希/签名及操作指令仍不作为查询字段。

恢复时 `git status` 确认未提交改动保留；专用 PG `127.0.0.1:32882`、Vite
PID `4008051` 复用。2026-10-20 再次 fetch 得 `origin/main=1a743c1`，
已含 #425 (`58201e4`) 与 #426。按用户后续指令：本功能提交后普通 merge
origin/main，不 rebase、不 force，合并后针对实际受影响模块回归。

第三轮独立复审已核对：金额表达式统一使用 active override 的唯一 line_id LEFT JOIN，
不扩大行数；动态枚举取值经过项目范围与金额资格；V1/V2/旧明细分别保持原投影。
新普通导出先完整生成临时文件，再释放数据库连接并响应；客户端断开或失败会关闭文件。
CSV 复用既有门槛（100 万数据行、64 MiB 动态文本、512 MiB 文件），XLSX 为
1,048,575 数据行、64 MiB 动态文本、256 MiB 文件；超限统一明确拒绝，不返回截断文件。

开发期 Playwright 使用本机 Vite 与合成 API 响应，覆盖全局/项目页面 320px、390px，
实测文档宽度等于视口宽度。检查包含中文输入、长 PN、0..5 范围、第二页、
CSV/XLSX 相同筛选参数、403 后清除过期计数并禁用导出、清空、空集，以及
无 data_profit/data_purchase_cost 的本人项目负责人使用有效成本范围。
截图及网络参数记录在外部 laneB 的 `rowfilter-browser*.log`、
`rowfilter-global-viewer-{320,390}.png`、`rowfilter-project-manager-{320,390}.png`。
此证据属于开发期界面检查，不替代候选构建的用户实际点按验收。

末轮逐款复核另补两个公共 API 反例：

- 数值筛选端点不是金额写入，`[-1e12, 1e12]` 应可查询正常数量；仅拒绝非有限数
  与 PostgreSQL numeric 无法表示的精度/指数，不套用成本证据的有效金额边界。
- 项目直达权限与全局目录范围原本不同：未开项目行键且无挂靠的 canonical 销售，
  可直达本人项目，但可能不在原全局目录内。项目网页/目录/导出复用直达鉴权，
  全局仍保留目录范围（显式 project_id 也取交集），不相互替换查询集合。

合并前后端相关子集首轮 239 项通过（`rowfilter-backend-premerge.log`，525.23s）：
三个新增筛选测试文件，以及 project_master_workbook、demand_export_ordering、
line_identity、export_headers、download_object_semantics、cost_quality。
随后补的销售直达正例先失败（期望 30 行、实际 0 行），修复使用显式 direct_project
上下文，只在项目路径沿用直达范围；追加验证同时覆盖原全局仍为空的反向约束。
修复后 `test_maintenance_row_filters.py` 与既有
`test_maintenance_master_v2_editor_permissions.py` 共 31 项通过
（`rowfilter-green-direct-scope.log`，106.89s），独立复审确认各路径接线及默认值。
前端新增组件、PartsOrdersTab、三个主页测试及项目面板共 7 文件 140 项通过
（`rowfilter-frontend-premerge.log`，134.60s）；项目面板测试补齐字段目录 mock，
避免新增请求访问本机 API。类型检查通过；生产构建先前通过，合并主线后再次验证。
