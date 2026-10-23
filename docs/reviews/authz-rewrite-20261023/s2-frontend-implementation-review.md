# S2 前端实施与交叉审查

S2 前端已完成可集成实现，app/tests 已冻结交主代理提交与精确 CI。这里记录本地证据；不表示远端 CI、用户验收或生产部署已完成。后续维保与平台域仍按服务端 planned/legacy 状态显式展示，未借 S2 目录宣称全部业务已迁完。

## 实现边界

- F0：`backend/scripts/generate_authz_frontend.py` 从后端 executable definitions 与 manifest 生成唯一静态词表；当前 87 个已知资源、23 个执行资源、72 个 surface。普通用户不请求管理员 catalog。CI 同时检查前端词表和 Cerbos policy 的生成新鲜度。
- Provider 是唯一运行授权真值；资源/action/field/状态闭集验证。Rule 为完整 AND 子句的 OR；surface 为 display action AND 数据 Rule，菜单/路由只 OR `route_entry=true` 的真实入口。embedded-only 池参考面不会打开 `/parts` 菜单。未知、missing、suspended、requires_context 均失败关闭；仅服务器明确 legacy 的资源采用旧资格。
- `useAuthorizedQuery`、`useAuthorizedRunner` 统一捕获 token、snapshot version、generation、queryKey、AbortSignal。查询在 owner/query/rule 变化的首帧不返回旧数据，清理时 abort；写和下载只尝试一次，完成副作用必须仍属于捕获主体。503 失效整个授权快照，不转换为空数据或重放。
- F1：销售执行词表已删除旧 read，分别使用 8 个数据动作和 12 个显示动作；概览可独立 trend-only，histogram 还要求成本字段，导出/detail/reconciliation 各自受门。采购父 effect、选项、分组、明细与下钻按实际表面挂载；利润按 dimension 选择具名动作，导出叠加当前维度。旧 S1 测试来源通过显式 test-only upgrade helper 转换，运行 UI 无 read→show fallback。
- F2：公司采购、销售、池、KPI、纪律、逐单事实独立挂载与查询；共用池订单 Modal 必须指明资源合同。复合响应要求固定 block_access 与主体动作同时允许；pool policy、analysis、catalog 的字段不互借。locator-only 列表保留未知 total=null，使用单套前后翻页；仅能看 policy 时搜索明确仅池名，详情不把缺失成员误称零成员。
- F3 由独立作者实现，本代理交叉审查。主数据用 `/parts/master/search` 与 edit-state，不经 overview 借数据；query-key 清理、捕获信号、mutation 撤权测试通过。型号 overview 身份可 null；quick-pricing 的 OR 入口不能代替 finance/market 各自数据动作；响应单块与主体单块双门。查询 URL 切换的旧数据首帧与 abort 缺口已由真实 Provider/layout-effect 红例复现并修正。
- 控制面仍使用 S1 原子模块合同；requires_any_actions 依赖中文提示不会自动补授。`/accounts` 在快照 pending 时始终保留路由占位，避免旧 legacy 入口仍可用时 wildcard 把控制面深链导走；ready 后按 read_accounts 允许或拒绝。

## 金额口径共享依赖

`display_policy.read` 不会从业务 read、角色、模板分类或 show 自动补授。权限编辑器说明“全局展示与业务默认值（金额展示设置）”共享读取依赖。缺权、加载、网络失败有不同状态；policy owner 不一致时立即 unavailable，独立 layout-effect 反例证明没有一帧 DEFAULT 被当作有效口径。

| 路径 | 保护方式 |
|---|---|
| `/pools`、`/pool-analysis/:groupId`、`/profit`、`/maintenance/dashboard` | 实际按全局口径解释金额，使用整页 TaxPolicyBoundary |
| `/boss` | 命名展示谓词根据真实可见 surface 与同资源字段决定整页依赖；health-only 数量仍可用，excluded revenue 由 TaxMoneyByBasis 自身失败关闭；纪律摘要金额本来明确未税，逐单弹窗另保护其金额单元 |
| `/purchases/analysis`、`/purchases/exceptions`、`/purchases/records` | purchase_records 成本字段可见时要求口径；无成本时保留数量与身份、隐藏金额列/单元 |
| `/parts` | 采购/库存金额列要求本域成本字段与 taxReady，销售金额列要求 taxReady；财务/市场金额块局部拒显，身份/库存数量/互替关系保持独立 |
| `/inventory` | 成本字段与 taxReady 同时满足才显示金额列；数量始终按本域 Grant 可用，有成本但缺口径时局部提示 |
| `/governance` | 仅 PartsTab 的营收列依赖 taxReady并局部提示；其他治理/身份/质量队列独立 |
| `/master` | 批量规范化的 TaxMoney 单元自身拒显，主数据表单不被全页口径阻断 |
| `/accounts`、`/system-settings`、其余无全局口径消费者 | 不受全局金额边界阻断；销售分析、采购分析使用本地/API 金额口径，池管理使用输入价自带税基 |

跨页 PoolReferencePanel 在自身边界中等候可用口径；PoolOrderDetailModal 保留非金额事实，缺口径时不显示税额。全局 Router 只按 token 重挂；已迁页按 generation/version 清理。没有使用 display:none 保树来处理 portal 撤权。

## 检查证据

以下计数属于各自运行，存在重复测试，不相加宣称覆盖数。

| 检查 | 结果与日志 |
|---|---|
| 完整本地前端回归（最后局部金额/深链修订之前） | 170 files / 2005 tests passed；`/tmp/itsp-s2-full-frontend-frozen.log` |
| 最后局部税/池/库存/公司专项 | 9 files / 94 tests passed；`/tmp/itsp-s2-dynamic-tax-targeted.log` |
| 壳层、Provider 页面组合、控制面、API 合同 | 8 files / 58 tests passed；`/tmp/itsp-s2-final-shell-contracts.log` |
| 真实 App/Shell/Provider 最终组合 | 12 tests passed；`/tmp/itsp-s2-tax-final-combinations.log`。包括 accounts pending allow/deny、缺共享 read 控制面、inventory 成本两态、parts inventory-only、company health、采购三页 no-cost、治理局部营收拒显 |
| 治理与采购最后局部修订 | 8 files / 75 tests passed；`/tmp/itsp-s2-local-tax-governance-purchase.log` |
| policy-only 未知总数/搜索纠正 | S2BusinessAccess 11 tests passed；`/tmp/itsp-s2-policy-locator-final.log` |
| F3 独立作者专项 | 最终真实 Provider 15 tests passed；`/tmp/itsp-authz-s2-f3-local-tax-final.log`；原 Parts 两文件 40 tests passed；其他 F3 证据见 `s2-f3-implementation-and-cross-review.md` |
| Tax 第一帧独审 | 独立作者真实 Provider + layout-effect 原红例原样转绿；`/tmp/itsp-authz-s2-tax-first-frame-green.log` |
| 最终构建 | `npm run build`（含 tsc）通过；`/tmp/itsp-s2-build-closed.log`。仍有 Vite 已有大 chunk 提示，非构建失败 |
| 静态一致性 | 两个生成器 `--check`、`git diff --check` 通过 |

本地完整回归早期失败已定位：历史 isolated-page fixture 缺少显式授权快照、异步 capture 调度改变、以及 F3 query-key 修订期间采集到中间状态。实际授权、安全、迟到响应断言保留；新版 canonical 反例使用真实 Provider。最新局部修订的完整组合交主代理精确 CI，不用前版 2005 绿冒充最终远端结果。

## 精确 CI 后的测试接缝修订

`50bbe55` 的完整 CI `40019408836` 前端为 2016 passed / 1 failed。失败在 `AppAuthorizationDependencies` 首个库存数量用例：RTL 的默认 1 秒 `findByText` 同时承担首次路由模块转换、Provider 提交、业务请求和表格渲染。CI 首条耗时 1383ms，下一条相同库存页面的成本字段变体通过；本地原样 12 项通过，首条也达到 1104ms。生产授权、查询和金额保护代码没有因此修改。

诊断时仅临时将**真实** InventoryPage 模块 import 延后 1.2 秒，原断言同位置复现 1 failed / 1 passed；等待 `NAV_ITEMS` 与 React.lazy 共用的原 `.load()` promise 后，同一探针 2 passed。日志为 `/tmp/itsp-s2-app-lazy-delay-red.log`、`/tmp/itsp-s2-app-lazy-delay-green.log`。临时延迟已删除，最终测试无固定 sleep、无增大全局 timeout，也没有以重试通过代替首次结果。

最终 diff 只为该测试显式等待真实模块加载，并新增初始无请求、`/inventory/dynamic` 恰好一次、正确 params、捕获 token 和 AbortSignal 的断言。原 PN/仓库实际可见、成本列与金额不显示、金额依赖提示两态、零 settings 请求均保留。完整文件用 6 个独立冷进程、最多 2 个并行重复验证，六次均 12 passed；`/tmp/itsp-s2-app-dependencies-stress-{1..6}.log`。`tsc --noEmit` 通过，日志 `/tmp/itsp-s2-app-dependencies-tsc.log`。这些是重复稳定性证据，不新增 72 个独立覆盖用例。

前一次 `BusinessTimestampSites` 的 TaxBasis 全量 mock 缺新 hook 已在 `50bbe55` 修为保留真实导出的 partial mock，并使用合法 `ex` 值；两条跨午夜日期输入和断言不变。原文件 2 passed、治理组合 4 files / 8 passed、独审原样 2 passed。此处记录测试适配，不将其视为生产行为修订。

## 复核结论与剩余边界

根代理审 F2 发现并修复 analysis/policy 治理字段混用、policy-only 缺失成员与 total 语义；独审发现并修复 Tax owner 首帧；本代理审 F3 发现并修复 URL 首帧/query abort 与 quick-pricing OR 误用。新的反例均通过相应专项。生成的 72 面闭集是入口协议证据，不能替代所有 Grant 组合的穷举测试；本轮重点反例覆盖独立授权、字段两向量、共享数据、直接深链、撤权、503、迟到与导出完成提交。真实隔离浏览器由主代理记录；未做生产发布或用户最终验收。
