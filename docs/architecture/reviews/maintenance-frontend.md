# 维保前端架构复核

范围：/tmp/itsp-maintenance-modularity，基线 7a9fd20 + 当前后端候选。前端业务代码最初只读；反例测试在独立 /tmp 副本。证据等级：A=组件测试复现；B=代码路径及跨域核对；C=待验证方案。没有生产/API真实写入。

## 结论
已有真实扩展 Seam：panel 注册读回屏障、dashboard 共用筛选及会话所有权、row filter 服务端元数据。优先修复上传预检的跨项目生命周期漏洞；随后统一这个已被多个业务复制的异步操作协议，而非先把全部 Tab 做成插件。

## 确认问题 F1（A，既有缺陷，非本批后端候选引入）
AcceptanceTab.tsx:169-185 在 beforeUpload 的 await preview 后没有核对项目或操作世代；:114-164 的 Modal.confirm/onOk 也没有。组件 A 发预检后切换 B，A 返回会弹出应用 A 批次确认；用户在 B 界面可能误改 A。父页面 MaintenanceProjectPanelPage.tsx:579 没有给 AcceptanceTab project key，且同一项目路由参数切换保留父组件，不能依赖必然卸载。
独立 Vitest：frontend-repro/src/pages/maintenance/panel/__tests__/AcceptanceTab.test.tsx 最后用例；运行命令：./node_modules/.bin/vitest run src/pages/maintenance/panel/__tests__/AcceptanceTab.test.tsx -t 'late preview'。acceptance-repro.log：1 failed，切 B 后预检 A 仍生成确认标题（预期0，实际2个标题/无障碍节点）。仅模拟组件/网络响应，未调用真实写入API。
对照 CollectionTab.tsx:109-119 已有 modalProjectRef/dialogEpoch，必须在 await 前后验证所有权，旧 finally 也不得覆盖新提交状态。
backend_review 交叉确认：api/maintenance_acceptance_checklist.py:197-201 按 batch.project_id 校验scope；service apply_batch校验owner并修改批次所属项目。所以这是双项目都有权限时的旧项目误操作，不是越权漏洞。
回归：预检中换项目/卸载；已有确认时换项目；apply中换项目；同项目旧预检迟到；成功apply不得触发新项目读回；失败不宣称未写入；保留同文件重选。

## 范围与状态所有权（B）
- nav.tsx:87-94,195-233,297-302：5个主页面入口与稳定项目详情路由；主页、分析、看板、需求单的权限不同。
- MaintenanceHomePage.tsx:112-161：URL持有筛选，输入框持有草稿；:178-215：候选分页total与过滤后rows长度不同，不能复用普通空页停止逻辑。
- MaintenanceProjectPanelPage.tsx:230/313：稳定项目/board loader 与workspace loader分别负责备件金额与回款/费用/领用，函数式更新避免覆盖；:383-401：Map注册已挂载Tab，Promise.allSettled等待真实读回，不等同于广播事件。
- panel/OverviewTab.tsx:52-82：候选挂靠读取及注册；PartsOrdersTab.tsx:25-35/panelUtils.ts:150：useLatestRead让被新读取取代的旧屏障追随最新读取；ExpenseTab.tsx:34-60：失败清空并注册；CollectionTab.tsx:128-152：计划读取，回款快照由父页持有；SiteReturnTab.tsx:164-170：领用读取与返还子区域各自注册；AcceptanceTab未参与总表读回注册，但自行load验收清单；分析定位Tab按需挂载，明确只表示分析筛选。
- MaintenanceDashboardPage.tsx:224-256：options取权限/允许排序并绑定ownerToken；:262-311：统一筛选；:491-515：5个Tab按需挂载；dashboardShared.tsx:24-52：ownerToken为会话归属，序号为请求归属，两者不能相互替代。
- DashboardProjectsTab.tsx:107-141：并行项目与orders读取，orders显式移除q/ratio；DashboardPartsTab.tsx:150-184：按金额权限选择valuation/parts两个端点；Overview/Consumption/QuantityChecks/ReceiptsDrill各自序号与会话守卫，不应把所有业务聚合改成统一字段累加。

## 权限与金额（B，已交叉排除误报）
ExpenseTab.tsx:90-95始终声明双税金额列，乍看缺金额门，但backend_review确认expense-rows路由在api/maintenance_project_master_workbook.py:347通过_require_profit_visibility，后者在api/maintenance_expense_collection_workbook.py:96-111只允许admin或runtime_safe(data_profit)。不是数据泄漏；可能出现可见报销Tab但403的UX，需与产品确认是禁用提示还是隐藏。
components/maintenance/maintenancePermissions.ts 与面板:210的can_edit_master_workbook不可压成一个canViewAmount：采购成本、利润、项目编辑资格、显式实名高风险action独立；管理员也不总是绕过显式action。
DashboardOptions.amounts_visible是服务端权威，数量账号不渲染金额列、试算参数或金额导出；混合单位数量/比例必须保持null并展示unit_breakdown（dashboardShared.tsx:162-210）。D-0024禁止跨三参考域相加。

## 刷新扩展风险 F2（B，未宣称为错误）
utils/maintenanceRefresh.ts:8只发不含业务内容的信号，:21实现可见性/节流/跨Tab；全生产代码只有MaintenanceProjectPanelPage.tsx:390发布。订阅仅analytics、PnCompactView、RankingsExplorer三处；主页/新看板不订阅，其他独立写入口不广播。新增写入口若只调用自己的load，不会通知异页分析。新增只读页若认为“维保数据变化自动刷新”也会误判。
回归建议：每种已成功业务命令只发一次信号；刷新失败不可重发写命令；跨Tab/同页合并；隐藏页不轮询；筛选/会话变化不能回填旧结果。先定义需要即时更新的页面清单，不擅自改无限加载主页刷新体验。

## 新功能变更面（B/C）
新项目Tab通常至少改：新Tab文件、父页lazy+items、analysisNavigation.ts:13-21白名单、对应API client、Tab测试、父页/导航测试（6类，通常5-7文件）。有写动作需接PanelRefresh并处理异步生命周期。全局路由新页另改nav.tsx。
新看板Tab通常改：新Tab文件、MaintenanceDashboardPage.tsx的TAB_KEYS/TAB_LABELS/DEFAULT_SORTS/items四处、maintenanceDashboard.ts types+端点、后端options排序/导出、页面测试（至少5类文件）。若已确定未来多个Tab，可使用typed Tab descriptor收拢这四处；目前只是局部结构重复，无需动态插件加载。
新行级筛选：后端field registry提供字段/运算元数据，前端RowFilterBuilder + maintenanceRowFilters client已有适配，不应再硬编码每个业务字段。新增复杂输入运算才需要前端变化。
新动作批量：CollectionBatchMaintenance/SiteIssueReturnBatch/ReturnReceiptBatchMaintenance应复用逐行回执与失败保留协议，不能把页面批量宣称为整批事务。

## 分批方案
1. 修AcceptanceTab生命周期并补反例（小范围，无API/业务决策变化）。
2. 选Acceptance/Collection两个真实Adapter提炼上传操作所有权及确认生命周期Module；保留各业务预检、幂等、回执，禁止泛化为任意业务写插件。
3. 按用户确认范围补刷新发布/订阅契约，保留屏障与广播区别；API层不直接依赖React状态。
4. 在第二个新Tab需求出现时将metadata注册集中，保留每Tab独立 Implementation和API契约，不统一加载语义。
5. 权限能力呈现按服务器能力细分，尤其expense-rows 403 UX；不把能力词典当安全授权层。

## 与当前候选关系
scope/assigned_lines/audit/ToolCatalog都在后端；没有改变此处前端接口/状态/刷新契约。已独立验证迁出的7个函数AST等价、18个工具schema fixture与HEAD一致。前端问题是扩大调查发现的旧问题，不应作为后端候选造成回归的证据。根代理随后明确授权小范围修F1。

## 实施与验证补记
根代理授权后只修改 AcceptanceTab.tsx 与新增 AcceptanceTabLifecycle.test.tsx，不修改后端、依赖清单或其他业务前端。项目/scope世代、组件存活、upload世代共同守卫；清理保存的静态Modal并结束待确认Promise；已发送apply不撤回，但旧项目响应不触发本地/父级刷新或新视图反馈。写后读回先失效旧清单。
agent_review补充同项目路径：V0显示→apply写V1→读回pending→新预检取代→旧读回不落地，必须失效V0，并按read scope/generation清loading；已加入专门回归通过。
依赖复用首次发现缺user-event，随后在隔离副本npm ci按仓库锁文件安装，不动工作树node_modules或package文件。早期相关测试143中1项备注toBeVisible失败；正确锁文件依赖后同组合143全部通过，未修改备注代码/测试。不能将早期失败算成通过，原日志保留。
最终：acceptance-fixed.log：2文件16 passed；frontend-related-tests-final.log：4文件143 passed；frontend-build.log：npm run build（tsc && vite build）exit0，仅既有chunk体积提示。合计159项相关测试通过。
完整静态目录：frontend-inventory.json，100个非测试维保命名TS/TSX文件，31,794行，20个API文件。该清单是全文导入/调用模式扫描；人工深入审查限报告列出的关键模块，不冒称31,794行逐行语义核验。
