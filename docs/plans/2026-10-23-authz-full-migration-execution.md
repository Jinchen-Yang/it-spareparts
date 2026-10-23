# 全模块权限迁移执行与验收账本

决策：D-0026、D-0027。跟踪：[#453](https://github.com/Jinchen-Yang/it-spareparts/issues/453)。基线 `7a9fd2098acb6be0009a59a84e7d7330bb960d6b`；首批候选 [#455](https://github.com/Jinchen-Yang/it-spareparts/pull/455)。

目标是迁完全部授权执行链，而非只补页面开关。继续复用已验证的 Cerbos SDK、SQLAlchemy adapter、现有业务查询及 AntD；不更换业务平台。生产保持不变，统一用户验收在全模块候选完成后进行。

## 阶段与依赖

| 阶段 | 完整交付 | 退出门槛 | 当前状态 |
|---|---|---|---|
| S0 首批框架与销售 | 销售全链、客户归属、通用 engine、真实引擎和数据库测试、筛选组件 | 现有独立复审及全量 CI | 候选已实现；最终 `16c38c3` 全量 CI 已通过：7,007 passed / 7 既有 skip，前端全绿 |
| S1 通用控制面 | 闭合目录与覆盖账本、主体加载、资源 Access、通用快照、账号/模板/关系单一编排、前端统一编辑器 | 身份与提权/并发/会话/模板三角复审，旧销售回归，精确提交 CI | 已完成；精确 `045969c` 全量 CI：7,094 passed / 7 既有 skip，前端 1,969 tests 全绿 |
| S2 商业与基础域 | 销售各面板、采购/利润/经营看板、型号/池/库存/主数据/替代关系/治理/设置、对应工具和导出 | 行及字段非干扰、旧规则差分、所有入口/工具闭合、三角复审及 CI | 正在实施与对抗审查；23 资源 / 72 面板接入，尚未通过完整 CI |
| S3 维保关系与全部读写域 | 项目目录/汇总/详情、分析/看板、合同/费用/回款、需求/领用/返还/实收/残值、工作簿/验收/仓库/迁移/补库、文件和任务 | D-03/D-25例外、对象越界、锁内撤权、工作簿及批量事务回归、三角复审及 CI | 未开始 |
| S4 跨域收口与统一候选 | 通用导入、Agent/聊天历史、共享文件、后台任务、服务接口、遗留路径退役、全量迁移演练与重构对比 | 清单无未解释项、权限反例及迁移差分、全量三角复审和 CI，随后统一用户验收 | 未开始 |

S2/S3 资源附带的工具、附件、任务在该资源迁移时同步接入；S4 验证跨域生命周期，不能用它推迟已迁域的必要保护。同一阶段按不相交文件分工，目录/核心/壳/迁移链各由单一负责人统筹。每阶段完成并全绿后再开始下一阶段实现。

## 覆盖分母与证据

当前运行时基线为 391 个 HTTP method/path、18 个 Agent 工具；前端为 24 条业务路由、30 个历史重定向。32 个业务组用于组织工作，不等于资源数量。2,829 个显示源码出现项是追踪索引，不是独立权限数或完成数。

最终覆盖账本需逐条登记以下关系，并对新入口和未登记项失败：

| 入口/合同 | 核心记录 | 证据 |
|---|---|---|
| HTTP与机器入口 | method/path、主体类型、资源动作、公开/认证/退役具名理由 | 运行时集合相等；允许与拒绝测试 |
| 面板与交互 | route、surface、实际组件、数据来源、动作/信息组 | 未授权不挂载查询；对象与会话不串用 |
| 查询和字段 | 稳定对象关系、各事实查询、字段用途/推导依赖 | 范围先于聚合分页；隐藏字段非干扰 |
| 工具/worker/文件 | 领域执行服务、执行及发布重检、产物来源合同 | 替代入口拒绝、排队及下载撤权 |
| 迁移和编排 | 旧有效规则、新Grant、模板与个人调整、例外依据 | 冻结解释器差分；不扩权；并发原子性 |

调查输入：[后端地图](../reviews/authz-rewrite-20261023/backend-next-stage-map.md)、[前端地图](../reviews/authz-rewrite-20261023/frontend-next-stage-map.md)、[独立对抗计划](../reviews/authz-rewrite-20261023/full-migration-adversarial-plan.md)。这些是方案证据，不能替代各阶段运行验证。

## 审查与最终比较

每阶段记录精确 SHA、实现文件、已闭合资源、发现编号、复现步骤、修复与测试、独立复审人及 CI 链接。测试不能以无关预期更新、skip、宽松 mock 或降低分支保护通过。

最终对比至少包含：旧角色硬门与隐式允许的减少、唯一授权写入口、可复用接入接口、API/工具/任务/文件的一致性、字段与范围配对、撤权有效性、存量变化清单、实现/测试/依赖/部署增量，以及仍不能自动证明的业务关系正确性。CI 全绿与用户验收、可合并与可生产分别报告，不能相互代替。

## 阶段记录

### S0 生成物修复

远端前端全绿；后端 6,937 条通过、7 条跳过，仅 `test_dependency_locks_and_cyclonedx_sboms_are_fresh` 失败。修复按原测试要求移除 requirements 导出头并重生后端 SBOM，未改依赖版本、uv.lock、测试或生成器，前端 SBOM 未变化。原失败测试本地 1 passed；独立复审通过。新候选 `1be345dd2e6396714de692ac5765a6920d7a272b` 的 [CI](https://github.com/Jinchen-Yang/it-spareparts/actions/runs/39980104263) 该串行 run 后续结束：6,937 passed、7 skipped、1 failed，唯一失败正是下文已复现并修复的跨午夜夹具；依赖生成物失败未再出现。后续四分片精确提交结果见下。

### S0 完整回归分片

为保持每阶段全量测试且缩短等待，提交 `edd0f3efade00ab457731737f82f68eb282a2226` 将后端按完整文件分为四个独立 runner/数据库，保留全部旧检查与 required 名称。新增的清单及执行证据汇总拒绝缺片、漏用例、越界跳过和失败清理；固定六个包含 ZIP 时间戳的参数化测试 ID，未改其输入或断言。新旧收集 7,014 个节点逐项、顺序一致，388 个测试文件全覆盖；分片 1,754/1,754/1,753/1,753。实现者测试、独立 CLI 对抗复审与根代理旧流程/业务比较已通过，[精确提交 CI](https://github.com/Jinchen-Yang/it-spareparts/actions/runs/39985909771) 已全绿，required 汇总验证 7,014 节点逐项恰好执行一次：7,007 passed、7 条既有明确 skip。前端构建、行为测试和前后端依赖扫描均通过。这项不是其余业务模块的迁移进度。

### S0 跨午夜夹具修复与最终门

本地串行诊断回归 6,982 passed、7 skipped、1 failed（收集时 6,990 节点，不能冒充新清单 7,014 全量证据）。唯一失败是维保迁移来源夹具在收集与执行跨上海午夜时使用不同日期；真实 pytest 注入前一天默认值独立复现。提交 `16c38c3063c2f971d3c0cce65f02924f1250e053` 将默认水位与本次快照日期绑定，同一 hash 对比显式固定日期；保留 None 缺失语义和原断言。修复文件 18 passed，跨模型独立审查通过；最终 [CI](https://github.com/Jinchen-Yang/it-spareparts/actions/runs/39988713589) 已全绿，必需汇总确认 7,007 passed / 7 既有 skip，前端全绿。

S1 wire、5 份共 45 个 DTO 样例已完成前后端对拍和独立设计复核，无剩余设计阻断。S0 最新精确提交 CI 全绿后才开始 S1 app/tests；设计通过不能替代实现验证。

### S1 实施已开始

在 `16c38c3` 精确 CI 全绿之后，从该提交建立 `codex/authz-s1-control-plane`，保留 S0 PR #455 候选。控制面、前端和核心分文件并行实施，独立审查者拥有独立对抗测试文件。核心采用 typed identity、实时账号与 Grant、按资源配对的批量 PDP、持久 cutover 启动事务；当前均为本地候选，未通过 S1 最终门。

### S1 本地阶段门与 CI 候选

已完成通用主体、资源访问、批量快照、持久切换、规范账号/模板/批量编排和统一前端编辑器。根代理对控制面、控制面作者对核心业务/切换、跨模型独审对真实 HTTP/PDP 及前端会话分别审查。修复了来源 JSON 默认值、非法建号与载荷、旧错误协议、对象上下文缺失、窗口关闭后本人撤权、预览展示、自助改密等锁撤权，以及目录漂移造成 cutover 后旧允许复活等问题。

终轮本地后端组合 268 passed（`/tmp/itsp-authz-s1-all-targeted-final.log`），涵盖 core、管理、独立反例、销售集成和旧账号/权限中心/改密；其后第三角新增两例通过 `pg_blocking_pids` 观察真实等锁再撤权的用例，独立 2 passed。另旧销售 163 passed。前端完整一轮 164 文件、1,964 tests 通过；随后新增中心 3 例和独立第 7 例均在专项组合通过。最终 TypeScript + Vite build 通过。这些不是所有后端测试，也不是最终精确提交 CI 的替代证据。

实施/复审记录：`s1-management-implementation-report.md`、`s1-root-control-plane-review.md`、`s1-implementation-independent-review.md`、`s1-core-business-cutover-review.md`（位于 `docs/reviews/authz-rewrite-20261023/`）。当前运行 396 个 method/path，目录为 1 implemented / 84 planned / 2 retired；S2–S4 未实施，不能据目录条目数宣称已迁完。S1 精确提交 CI 全绿之前只做下一阶段只读准备。

### S1 后续迁移来源修订

下一阶段的只读检查发现：规范建号 `none` 和规范空模板，与部分旧对象一样可能没有 Grant 或有效包，后续 seed 不能以空值判断旧来源。S1 补持久的旧迁移资格标记，规范创建/复制默认不具备旧种子资格，旧存量与旧创建入口明确记录来源；已有规范资源仍优先保留。该修订须通过独立迁移/越权测试及新精确提交的全量 CI 后才能进入 S2。此前 `bad6976` 的 CI 仅能证明修订前候选。

修订已完成：规范/旧来源明确区分，降级先锁相关表再检查，拒绝擦除 cutover 或非旧来源。作者来源与管理组合 82 passed；独审与三个旧回归文件组合 84 passed；历史降级调用的完整选集最终 123 passed（`/tmp/itsp-authz-s1-all-downgrades-final.log`）。修订前完整 CI 的 10 个失败均已定位为旧测试接入问题并保留原断言修复；新增来源守卫所影响的旧 fixture 明确标注来源，另修池迁移未依赖标准数据库清理导致的顺序污染（前置维保历史 + 全池文件 16 passed）。这些补充结果须由修订提交的新全量 CI 再确认；S2 仍未开始实现。

`14f589f` 的完整 CI 另暴露一个前端异步断言：批量改权冲突文本已进入 DOM，但 Modal 入场期间尚不可见，前端为 1 failed / 1,967 passed。确定性隐藏→显示反例在原断言下失败；修订只将即时可见断言改为等待实际可见，保留禁用执行、两次请求、未成功回调，并补禁用点击不重放和未关闭窗口。作者 5 文件组合 36 passed、跨模型独审单文件 13 passed；生产代码未改。该测试修订仍须新精确提交全量 CI 通过，不以此前的通过结果代替阶段门。

同轮后端三个分片通过，分片 1 的三个失败均是池序列迁移 fixture 仅依赖 `migrated`，未清理前序已提交维保台账。真实台账应用用例作为前置，再运行池序列整文件，修前 3 failed / 2 passed，改为依赖标准 `db` 后 5 passed；原序列高水位、恢复和迁移断言不变，未修改生产降级保护。根对测试函数的同文件传递 downgrade 调用与 fixture 闭包做静态检查，修订后只有纯 offline SQL 渲染用例不依赖 `db`，该扫描范围内未发现其他同类在线迁移缺口。前后日志为 `/tmp/itsp-authz-s1-pool-seq-ledger-{before,final}.log`；仍待合并这两项测试修订后的新精确提交 CI。


### S1 最终完整 CI 阶段门

最终候选 `045969cfabf118b472a89abfb77258040c120326`（tree `86728a12db47ebc402fd23af09ab88feb836a129`）[完整 CI 40005416419](https://github.com/Jinchen-Yang/it-spareparts/actions/runs/40005416419) 全绿：四分片与聚合核对 7,101 节点恰好一次，7,094 passed、7 条既有 skip；前端 165 文件、1,969 tests、构建和扫描通过。[PR #469](https://github.com/Jinchen-Yang/it-spareparts/pull/469) 保持 draft、未合并。此后才从该提交建立 `codex/authz-s2-business-modules` 开始第二阶段实现。

### S2 实施与复核中

第二阶段迁移 23 个执行资源与 72 个展示面，拆分销售八类数据能力，接入商业分析、采购、利润、库存、公司看板、商品池、主数据、替代关系、治理及显示设置。API 与 Agent 共用领域授权服务，批量组合读取区分缺 Grant 的块拒绝与基础设施故障；独立资源的字段不合并。Agent 消息和文件记录服务端授权来源，发布、重放、下载重新验证。

存量迁移使用冻结的旧规则解释器、显式旧来源标志及版本化 receipt；保存模板快照、个人拒绝覆盖及来源键存在性，升级/可证明的降级均保持计数单调。控制面与历史销售回归分别完成 154 和 212 条；B1 旧业务回归 161 条。它们是阶段开发证据，不能替代最终完整 CI。前端与其余领域交叉审查进行中，S3/S4 尚未实施。

### S2 冻结候选与交叉审查

第二阶段按三个可独立阅读的增量提交组织：`0d3bc5e` 为目录/核心/策略/保守迁移，`a2e348f` 为领域应用服务与 Agent 来源，`036db66` 为前端面板与共享访问协议。它们共同构成阶段候选，不能拆出中间提交独立启用。S1 尚未合并，因此 PR 相对 main 的累计 diff 含前置阶段；S2 独立比较基线为 `045969c`。

领域入口闭合为 88 个实际 method/path，逐条无 Grant 拒绝测试及语义反例覆盖；B2/B3 最新领域三文件由根代理独立复跑 43 passed，历史迁移 41 文件 / 424 passed。三角审查修复了逐行事务、锁前旧对象解析、拒绝块 SQL、独立资源隐含总门、策略回执投影、Agent 跨切换文件与锁序等问题。前端独立审查以真红测试修复首帧旧对象/旧口径、跨资源字段借用、隐藏模块请求及账号深链加载竞态。

前端本地完整回归为 170 files / 2005 tests；最后组合修订另有真实 App/Shell/Provider 12 条及相关领域专项通过，最终 build 与两项生成物漂移检查通过。真实浏览器验证独立策略保存和无税口径权限的账号管理。详细证据与重叠计数边界在 `s2-root-cross-review.md`、`s2-b2-b3-implementation-review.md`、`s2-f3-implementation-and-cross-review.md`、`s2-agent-lineage-implementation.md` 及前端实施报告。

当前等待最终精确提交完整 CI。S3 已进行入口与对象/文件合同的只读准备，没有实施；S4 未实施。没有生产操作，没有用户统一验收，尚无全模块可合并结论。

### S2 完整 CI 的夹具接入修订

`47fd3f2` 的 [CI 40017516780](https://github.com/Jinchen-Yang/it-spareparts/actions/runs/40017516780) 前端为 2,016 passed / 1 failed，构建与生成目录检查通过。唯一失败是 `BusinessTimestampSites.test.tsx` 的旧整模块 TaxBasis mock 未包含新 `useTaxDisplayPolicy` 导出。原样复现 1 failed / 1 passed 后，改为保留真实导出的部分 mock，并使用既有 hook 的合法 `ex` 值；两条日期输入及北京时间断言保持不变。作者原文件 2 passed、治理组合 4 文件 / 8 tests passed、TypeScript 检查通过；跨模型独审原文件 2 passed。未改生产代码、未跳过或削弱断言。该修订仍需新精确提交的完整 CI。

根代理补做隔离浏览器的 trend-only 账号验证：仅授 `sales_analysis.read_trend/show_overview`，销售页面只出现概览/趋势；网络记录中销售业务请求只有 `/sales/analytics/trend` 且返回 200，未查询 options、summary、groups 或 orders。证据为 `output/playwright/s2-trend-only.png` 与 `/tmp/itsp-s2-browser-trend-requests.txt`。这是开发验证，不是用户验收或生产证据。


### S2 后端顺序污染的复现与隔离

`47fd3f2` 完整后端回归的两个失败分片只有 8 项失败：7 条 BOM 文件测试与 1 条型号歧义测试；分片 0/2、迁移链及 ORM 检查通过。两组失败均以真实前置用例重现，生产身份门与型号解析器没有改动。

- BOM 局部 module fixture 绕过标准清表，前置 Agent 测试留下持久授权标记。以真实 marker 用例为前置，修前 7 failed / 1 passed；改为公共 `ctx(db)` 依赖标准隔离后作者和根代理独立均 8 passed，7 个业务测试函数 AST 不变。
- resolver 局部 module fixture 继承前一模块提交的 30,000 条无关目录行，却没有提供歧义型号样本。真实三万行遍历用例接歧义测试，修前 1 failed / 1 passed。改为扩展标准逐项隔离 fixture 并显式种入精确/变体/歧义样本，三项条件断言变为必检；其余 13 个函数 AST 不变。根代理与跨模型独审分别运行三万行前置加完整 resolver，均 17 passed。

证据：`/tmp/itsp-authz-s2-bom-root-review.log`、`/tmp/itsp-authz-s2-resolver-sequence-red.log`、`/tmp/itsp-authz-s2-resolver-sequence-final.log`、`/tmp/itsp-authz-s2-resolver-sequence-independent.log`。精确 `50bbe55` 的后续 CI 已确认旧 TaxBasis mock 失败消失，但新增首个库存路由冷加载断言超时，正在独立复现和修订；该提交仍不能作为阶段通过依据。S3 只有只读执行方案，尚未开始实现。


库存前端失败已用临时延迟真实路由 import 的探针重现：原断言 1 failed / 1 passed，等待 React.lazy 共用的真实 nav.load promise 后 2 passed。最终删除临时延迟，仅显式等待加载；保留可见性和金额保护断言，并新增初始无请求及业务请求恰好一次的 token/参数/AbortSignal 断言。六个冷进程各 12 passed、TypeScript 通过，不将重复运行累计为新覆盖数。测试修订等待独立复核及新完整 CI，生产代码未改变。

跨模型独立复核最终库存文件 **12 passed**（`/tmp/itsp-s2-app-lazy-independent.log`），确认没有生产改动、timeout 放宽或遗留延迟。BOM/解析器/库存三组修订合并进入下一精确提交完整 CI。
