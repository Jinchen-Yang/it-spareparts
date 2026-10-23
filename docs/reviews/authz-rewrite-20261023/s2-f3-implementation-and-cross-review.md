# S2 F3 实施与交叉审查记录

F3 作者为独立权限审查代理；Agent/F3 代码由根代理与另一前端作者交叉审查，本文件不自签这些实现。已纳入 S2 分支，最终阶段 CI 待完成；未部署、未进行用户统一验收。

## F3 交付

型号搜索、主数据、治理及其独立疑点/校准面板、内联编辑、批量规范化、共享 PartPicker 均接入既有 AuthzProvider / generated Rule / captured transport。页面、数据动作、字段和复合响应 block_access 分开判断；类型不明或权限未就绪时不借用旧角色允许。新增 `pages/f3/useModuleApi.ts` 仅封装共享 runner，不维护第二份权限状态。

- 型号 overview 接受独立获权块，identity 拒绝时安全消费 `part:null`。关系管理和内联编辑可使用自己的资源入口，不强迫授予型号描述读取。
- 主数据 create-only 不请求列表；规范编辑通过自身 editstate，canonical 路径不借 lookup read。治理各 tab 按自己的 surface 挂载；候选拒绝、合并、确认为独立型号分别绑定实际动作组合。
- 疑点按实际 purchase/sales side 判断 read 与 review；校准字段依赖来自生成合同。所有异步请求携带捕获 token / signal，快照或目标切换后丢弃迟到结果，写入不自动重放。
- PartPicker 保留防抖和稳定 part_id 返回，权限与关键词变化立即隐藏旧结果；调用方的 legacy 旧行为测试显式注入 pre-cutover 夹具，没有改生产默认允许。

验证：`/tmp/itsp-authz-s2-f3-clean.log` 为 12 文件 **108 passed**，其中真实 Provider 授权/撤权新反例 8 条；`/tmp/itsp-authz-s2-f3-consumers.log` 为 5 文件 **43 passed**（3 个维护消费者、时间展示、API captured transport）。既有事务、幂等、并发与迟到断言保留。`tsc --noEmit` 与本包 `git diff --check` 通过。局部结果不能替代完整前端 CI。

## 独立交叉复核其他作者

- 根代理 Agent R5/R6/R8：所有文件下载逐块重验，新权威稳定 owner_user_id 不再受历史 owner_sub 误拒；append/checkpoint 在会话和 actor 锁前记录持久协议标记，发布前仍在锁内重验。调用路径未发现预先取得 actor 锁；原样真实 PG/PDP 6 条通过，`/tmp/itsp-authz-s2-agent-root-cross-review.log`。其中两连接 NOWAIT 测试证明标记阶段不持 actor 锁。这是独立复跑根作者向量，不冒称新设计。
- B2 池复合块：facts-only、policy-only、公司独立销售/客户块及 policy 写回执原样 5 条通过，`/tmp/itsp-authz-s2-pool-cross-review.log`。SQL 捕获与禁止调用证明未授 summary/catalog 不被暗中查询；未发现新增阻断。
- F1/F2 静态复核销售细动作、采购分块、Profit 维度与导出双门、公司订单独立资源、池响应 block 与 snapshot 双门、共享 runner 导出副作用。最新实际 Provider 11 条原样通过，`/tmp/itsp-authz-s2-f1f2-cross-review.log`。PoolsBlock 字段归属修正已包含该次测试。
- 新发现：TaxBasis 切换 snapshot 的首帧可能把 DEFAULT 口径作为 ready 值显示。真实 Provider 独立新测试 `TaxBasisAuthorization.independent.test.tsx` 用 layout effect 观察，v1 的 inc → batched v2 / 新设置挂起时捕获 `ready=true,basis=ex`。修复前日志 `/tmp/itsp-authz-s2-tax-first-frame.log` 为 1 failed；已交前端作者修复，待原样复验。本项闭合前不能签署前端快照隔离完成。

## 交叉审查后的闭合补充

另一前端作者指出 PartSearch 手写 effect 清理不能保护 URL 变化的首帧。独立设计的真实 Provider/layout effect 反例确认此问题；同时响应 `profit_summary=allow` 在 market-only 的 OR surface 下错误挂载财务块也确认。两条原样测试修前红，修后绿（`/tmp/itsp-authz-s2-f3-firstframe-{red,green}.log`）。修复是按 query key 同步投影所属数据、每次查询独立取消；PN→canonical ID 保留正确所属转换；财务和询价块再分别 AND 自己的 data action。相关 3 文件 **52 passed**，原 30 项 PartSearch 行为断言全部保留。

Master 列表同类手写状态收敛到共享 `useAuthorizedQuery`，以 q/page 为 key，编辑草稿保护不变。新增同权限对象迟到、跨资源同名 cost 字段不借权、对象切换首帧/取消、主数据 query 首帧/取消，真实 Provider 文件现 **13 条**。最新 Master + 该文件组合 **34 passed**（`/tmp/itsp-authz-s2-f3-master-query-final.log`）。旧 Master token-race 的 Provider 替身改为挂载会话固定 owner；deferred list 在发送前发现 token 变化而取消，因此从“两个旧 token 请求”收紧为“仅已发出的 categories 请求”，原不回填旧行的断言不变。

TaxBasis 作者增加 policyOwner 与当前 identity 不匹配时的同步 loading；先前独立 layout 红例原样 **1 passed**（`/tmp/itsp-authz-s2-tax-first-frame-green.log`）。首帧错误口径已闭合。legacy 草稿在 policy pending 期间的保留由前端作者另行验证，不以本用例代替。最后 `tsc --noEmit` 与本包 diff whitespace 检查通过；等待冻结版本全量回归。

金额共享依赖补充：根审发现整页税口径门会拦合法数量-only 授权。Parts 改成金额列/块局部保护：采购和库存金额列要求本域 cost 字段与当前有效税口径，销售金额列要求有效口径；缺 shared read 只提示金额区域，库存数量、身份、替代关系不受阻断。真实 AuthzProvider + TaxBasisProvider 的两项反例覆盖 cost=false/true 且缺 shared read：数量行可见、金额不显示、不调用 settings。Provider 文件现 **15 passed**，原 PartSearch 两文件 **40 passed**，`tsc`/diff 检查通过（`/tmp/itsp-authz-s2-f3-local-tax-final.log`、`/tmp/itsp-authz-s2-f3-local-tax.log`）。这些局部计数有重叠，不相加作为全量结果。

CI 夹具修订独立复核：`BusinessTimestampSites.test.tsx` 只将 TaxBasis 整模块 mock 改为 `importOriginal` 部分 mock，保留新增真实 hook 导出，并把旧非法 `ex_tax` 改为类型允许的 `ex`。治理历史和账号最近登录两条输入时间、预期北京时间 `2026-10-02 00:05`、查找/断言均未修改，也没有跳过用例或新增延时。独立原样复跑 **2 passed**，日志 `/tmp/itsp-authz-s2-business-timestamp-independent.log`。该最小测试修订可进入新 CI；不据此代替精确提交完整 CI 或用户验收。

CI 冷加载夹具修订独立复核：`AppAuthorizationDependencies.test.tsx` 仅对库存两例在 render 后等待 NAV_ITEMS 的真实 Inventory dynamic import factory，仍通过真实 App、Provider 与页面发请求；未 mock 页面、延长全局 timeout 或移除原金额/数量/缺权断言。新增首帧零业务请求与完成后精确一次 URL、分页参数、captured token、AbortSignal 断言。核对受控 1.2 秒模块延迟红绿日志（原版 1 failed / 1 passed，修后 2 passed）；独立原样完整文件 **12 passed**，日志 `/tmp/itsp-s2-app-lazy-independent.log`。无生产代码变动，无遗留定时器；可发布该最小夹具修订进入完整 CI，不替代精确提交 CI 与用户验收。
