# D-0026 首批权限实现独立复审

审查日期：2026-10-23（实时时钟核验）。基线及当前 HEAD：`7a9fd2098acb6be0009a59a84e7d7330bb960d6b`；审查对象为该基线上的未提交候选。工作树：`/home/cloudlay/Code/it-spareparts-authz-audit`。仅写本报告；未改业务源码、生产或外部系统。

## 结论

最终候选未发现尚未修复的阻断问题。发现并实际复现一项继承自旧销售字段政策的 P1 组合泄漏，主实现已在本轮修复，独立复测通过。此结论限于本批权限核心、管理接口、销售分析八个入口及其 SQL/字段接入，不代表其他 legacy 模块已迁移，也不代表生产可发布或全部安全性质已证明。

## 已修复发现：P1 对账权限绕过成本金额关闭

- 新授权依赖登记位置：`/home/cloudlay/Code/it-spareparts-authz-audit/backend/app/authz/catalog.py:36`。
- 原泄漏出口：`/home/cloudlay/Code/it-spareparts-authz-audit/backend/app/services/sales_analytics.py:599`；旧字段组归属：`/home/cloudlay/Code/it-spareparts-authz-audit/backend/app/config.py:904`。
- 最小复现：实名账号有 read、assigned_customers 和一个已分配客户，字段仅 `data_reconciliation`。合成订单含税额 1130，对账快照订单额 1130、已对账 300、待对账 830。真实 PDP → HTTP `/api/sales/analytics/orders/{id}` → 独占 PostgreSQL 返回 `amount_inc_tax=null`，但 `reconciliation.order_amount_at_snapshot=1130`，历史快照也返回 1130。即使仅遮蔽快照订单额，300 + 830 仍可还原 1130。
- 来源：这是旧领域字段政策的既有组合缺口，不是新增 SQL 范围遗漏；但新 grant 明确提供独立字段配置，不能继续宣称关闭成本金额即可阻断此组合。
- 最小完整修复：仅在新资源 grant 中增加 `data_reconciliation → data_purchase_cost`；管理输入和运行时都拒绝非法组合；存量迁移在成本关闭时撤销对账字段，不自动增授成本；前端提示相同依赖。
- 最终源码已实现上述修复。迁移收紧位置：`/home/cloudlay/Code/it-spareparts-authz-audit/backend/alembic/versions/f1a3c5e7b9d2_authorization_sales_customers.py:83`；前端提示：`/home/cloudlay/Code/it-spareparts-authz-audit/frontend/src/pages/accounts/SalesAuthorizationDrawer.tsx:55`。旧权限投影函数保留原语义，收紧仅发生在新表种子生成。
- 修复后独立探针：`ReplaceAuthorization` 拒绝 recon-only 输入；直接制造存量非法 grant 后，八个销售入口全部 403，`/authz/me` 的 read 为 false。证明管理校验之外，运行时也不会接受该组合。

## 其他核查结果

| 关注点 | 观察证据与边界 |
| --- | --- |
| 实名与 fallback | `_load` 要求 sys_user、非 fallback、有效实名记录、active、未软删除及实时 token_version；共享身份和缺 grant 无旧权限回退。 |
| grant/assignment 一致性 | 读请求对 SysUser 持共享锁；配置写以稳定 ID 顺序对 actor/target 取排他锁，并再次核验管理员。授权、归属、审计和 token_version 同事务提交。未发现可把旧 grant 与新归属交叉组合的管理路径。 |
| SQL 范围 | 八个 API 入口均执行 `_authorize`；列表/汇总/比较/分组/趋势/直方图/导出走同一 `_order_query`；options 的独立查询显式带条件；详情先经范围内订单查询，再读该订单行及快照。历史 lifecycle 也附相同条件，客户规范名相同不会授权另一个 customer_id。 |
| 字段与利润 | 仅 `data_purchase_cost`、不授 `data_profit` 的真实详情探针：毛利和毛利率为 null，行响应不含 cost_amount/cost_moving_avg。虽然查询内部读取成本用于计算，被隐藏的源值没有进入响应；未复现从该详情直接还原利润。排序、过滤、分组与响应投影继续使用显式字段 context，legacy RBAC 关闭不撤销该投影。 |
| 未知资源/属性 | 资源与动作白名单、查询绑定资源及属性集合均校验。独立适配探针中未知 plan 属性、未知 operator、错误 customer_id 常量类型均抛 AuthorizationUnavailable；缺 filter 编译为 SQL false。 |
| 开关 | suspended 仍使新权威 enabled，业务 API 503，快照无 read；PDP 故障不回退旧允许。部署切换关闭仍代表尚未迁移链路，不能用它作为已上线新授权的回滚方式。 |
| 迁移 | 仅旧有效页面资格产生种子、明确客户归属初始为空、角色范围按 D-0026 收紧、未知字段不迁入；新账号无自动 grant。仅建立新表，不改订单事实。 |
| 运行依赖 | 安装版本与 uv.lock 一致：Cerbos SDK 0.16.1、adapter 0.4.0；锁文件增加 22 包，既有包版本零变化。CI 安装固定校验和二进制，Compose 固定镜像 digest、只读政策目录、内部网络、无宿主机 PDP 端口。 |

## 独立执行的验证

1. 源码、契约和 D-0026 对照；`git diff --check` 通过。
2. 真实 adapter 的四类异常计划内存探针：未知属性、未知运算符、错误 ID 类型失败关闭，缺 filter 为 false；本探针模拟 PDP 响应，不能计作真实 PDP 测试。
3. 使用既有 `test_authz_sales_integration.py` 的合成订单及 PDP fixtures，通过内存 pytest plugin 注入单个独立测试，未留下测试源文件。PostgreSQL 基础入口为已指定的 loopback 32769 容器，pytest 各次创建独占测试库并清理；PDP 使用指定的 0.57.0 二进制。
4. 修复前真实字段组合复现与利润负向检查：`1 passed, 1 warning in 10.57s`。修复后八入口拒绝、输入校验、快照及利润负向检查：`1 passed, 1 warning in 11.05s`。中间两次探针因合成快照缺 content_hash、groups 请求缺 group_by 失败，均修正测试设置后重跑；不属于产品失败。

没有重复主实现已运行的整套测试；本报告不把主代理转述的测试数量当作独立执行结果。前端仅核对本发现相关依赖提示，未作完整 UI 审查或用户点按验收。没有访问生产数据、运行生产迁移、检查线上身份或执行生产部署。

## 最终候选关键文件指纹（SHA-256）

```text
0b9c18ba8dfba2ac98b63896f085e266b8f1c28082d67c6cf795134de14bca79  backend/app/authz/catalog.py
2d4a16161864ac2940ca03a3835cdb1a539b71f94d7fc617d1f3a350b3a4d871  backend/app/authz/engine.py
4023677dba0382f93dbb3f99d195e98c94c9914f5122aa6aed1ef2403d35a101  backend/app/authz/service.py
8386608835990293efa632f21a7763ae3d9ce9d0dd4d010d3e1e9d93a2dc856a  backend/app/api/authorization.py
2c9fe34673aa484c0826875491aa1ed5edb8bf3b4ee46575ef640cb6145cb7c7  backend/app/api/sales_analytics.py
22090d73a40e5d2e2457237b3baf18d14ba2263e4e01b29d803035cd80082d8a  backend/app/services/sales_analytics.py
aef03e939a6414f61ee6dd1ef297a0d4c39a30fbcf8213634a907d79f2949fec  backend/app/services/sales_dashboard.py
f033832b8b5a6c3500fd60d23e0df3d86cefc8c938617cfe9424aeb4de2ab80f  backend/app/security.py
c1dc94dcef8bcf36824d0cc086cd7a7ba45e3bbd7ae95e870c2910929eada932  backend/alembic/versions/f1a3c5e7b9d2_authorization_sales_customers.py
44100dc633fbe9ac5a631fd727c6f4efe29a4b174415bcab6ee9537e18f2bf4c  compose.authz.yml
ea5e0b35c2756ee5a8c07fb9db68baa2d2e78afc1f4e810cd0513936817ed41c  backend/uv.lock
ba4679facc00c1c44129cfd9b7a8f5a1042568a108573c78b8981cb7feee7393  backend/requirements.lock
```

## 补充复审：D-0025 销售人员复选筛选接入

在 selector_audit 确认修改稳定后，重新审查最终两页 FilterBar、PurchaseAnalyticsPage 的 owner 下传，以及共享 CheckboxMultiSelect、CSS、相关集成测试和 D-0025。对比既有独立工作树差异，合入仅涉及本批约定的两页销售人员筛选；没有覆盖销售页新增 AuthzProvider 门禁。

结论：本次接入未发现需要修复的问题。

- **草稿与确认**：打开复制已应用 value，toggle/search/reset 不调用 onChange；确认才复制 draft 提交。外部点击与 Escape 关闭后，再次打开从 value 恢复，未确认草稿不落 URL。受控值内容变化或禁用取消面板；确认还核对打开时 valueKey，防关闭 effect 执行前提交旧值。
- **候选刷新与名称**：draft 独立于 options。缺失候选的已选值保留；远程 onSearch 模式不把服务器返回结果再次本地过滤，knownOptions 提供旧已选标签。账号客户归属调用方累计已知 ID/名称，保留跨候选页选择。两页人员值本身是姓名，候选被替换后也不会只剩无意义编号。单选显示纯姓名，单量放 description；多选有数量、title 和完整已选列表。
- **会话边界**：销售 `FilterBar.tsx:224`、采购 `FilterBar.tsx:115` 在确认时直接调用 sessionStale；静默切换账号后不会把旧面板草稿写入新账号 URL，也不会以新身份触发查询。采购页新增两项 owner props 复用原页级会话 owner。
- **原有特殊语义**：AmountBasisSelect 未修改，仍至少保留一个有效口径；金额显示选择、金额区间条件、含退货开关及分组维度没有被通用复选组件替代。人员确认继续调用既有 patch 并归一分页，销售/采购各自 URL/API 参数名不变。
- **客户与授权**：客户筛选控件仍使用原实现；customerRestricted 的禁用和 effectiveState 请求收敛保留。销售页仍以新授权快照决定字段，服务端八入口仍独立鉴权；人员筛选只构成已有授权 SQL 范围上的附加条件，不能扩大负责客户范围。采购仍是 legacy，未把这次控件替换描述成采购权限迁移。

独立执行：

- `vitest run src/components/__tests__/CheckboxMultiSelect.test.tsx --reporter=dot`：12 passed，4.25s。
- 两页测试文件 `-t '销售人员'`：6 passed、76 skipped，7.45s。覆盖确认前零查询/URL变更、逗号姓名、分页归一、取消恢复、重置确认及静默切换会话拒绝提交。
- `git diff --check` 通过。

本次未重复全前端测试或 build，也未以 jsdom 结果代替真实浏览器布局与用户点按验收。旧独立候选的浏览器检查不算当前合并候选的用户验收。

补充范围最终文件指纹（SHA-256）：

```text
ea773636bc487c3b69df603d39f8c81ffee0408f0e1366871aea060cac02fbe9  frontend/src/components/CheckboxMultiSelect.tsx
74ea29be96163c214262562d176b54a303b6645c680000b632be1afd8c617cab  frontend/src/components/CheckboxMultiSelect.css
b29757460db11b8f878afffceaae35f99b1d558bc897d5c3bc46a97d832bf2bb  frontend/src/pages/sales/analytics/FilterBar.tsx
3f2aa1342800737fa725441652c42853acf8c31d64f0c449015d56534ddd5d0e  frontend/src/pages/purchases/analytics/FilterBar.tsx
71370bea36f445f93c52e0276b98b50c1e341bb5a143d817aad038dc8e483c59  frontend/src/pages/purchases/analytics/PurchaseAnalyticsPage.tsx
a41b108ef587dece28085ac0519006ae4664bb22387e8b5631e88d5d98a47b82  frontend/src/pages/sales/analytics/__tests__/SalesAnalyticsPage.test.tsx
89bf65bfb5b982495420e5c37a0f66ec88ad0dd0e64bd0f070d104824833d4a6  frontend/src/pages/purchases/analytics/__tests__/PurchaseAnalyticsPage.test.tsx
```
