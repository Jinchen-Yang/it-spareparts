# S4 前端实施与验证记录

状态：实施及本地最终验证完成，生产代码/测试已冻结；未提交、未发布、未进行生产操作。本记录不代替精确候选 CI 或用户浏览器验收。唯一协议仍是 `s4-agent-sse-wire-contract.md`、`s4-import-wire-contract.md`、`s4-platform-and-context-contract.md` 及后端实际 definitions/manifest/context registry。

## 实际 UI 与共同边界

S4 的 44 个已登记 surface 挂到三个现有路由：Chat 10 面、五源 Import 30 面与独立报销作废预演 1 面、账号目录/活动/资料 3 面。历史设计草案不是完成证据；实现直接消费 generated surface/entry。`rename` 等没有现成 UI 的 API 未为计数另造按钮。

- `/chat` 的列表、历史、新建、发送、attach、cancel、delete、上传、预览、下载分别判定；已知 `session_id` 不自动请求列表/历史或创建、运行。attach 不借 producer 的 use/run。停止、删除只在服务端确认后提交界面状态。新建/删除/打开目标统一失效旧 stream、upload 与命令捕获；同次 send 创建会话后继续该次命令，不伪造新的身份授权。
- SSE fetch 复用 Provider 与 captured runner，先同 entry 授权视图，再提交真实 precondition。ready 严格握手、run/seq 连续、单帧 1 MiB、未知字段/事件/错序/缺段/无 terminal EOF 全部拒绝。错误清 partial/trace，不把半截内容保存成成功，不进行任何 409 自动重发。图片 preview 不自动读取原件，download 独立动作且当前 capture 才能产生浏览器下载。
- `/import` 以明确 runtime 模式选旧/新 adapter，不按响应 shape 猜回退。precheck 原 v2 内容交原 parser；真实 source 由服务端 context 决定。准备与 upload 各走自身 view，precheck context 不授权提交。五源各自 show 与数据动作分开；只有预检/提交而无状态/报告时没有自动列表或轮询。独立 void preview 使用 `files[]`，保留 HMAC preview token 与授权 precondition 的不同作用。
- 已知 batch/job 可独立定位；二者同时出现在 URL 时明确报错且不猜目标。report-only 不需 metadata；error-only 直接自身下载合同。mixed job 的隐藏全局计数/状态与 input_index 保持 null；界面仅计算可见项，不把受限视为 0 或“未开始”。轮询错误即停止，重试由用户明确触发。
- `/accounts` 管理恢复按服务端 actor capability 进入 `/authz/accounts` 最小发现；普通目录仍要本资源 show+read。profile/activity 可用已知 username 单独打开，零目录发现；资料仅提交 display_name、无安全字段或伪造 epoch。活动 canonical 明确拒绝非 null resource/reason/before/after。坏单行 summary 显示不可用，不猜空授权、不阻断其他目标。canonical 不调用旧 `/accounts/_meta`。
- `boundaries` 是独立闭集元数据，不是 employee Grant。生成器从实际后端导出 107 executable 资源、112 module descriptors（含 5 retired）和 10 boundaries；worker/tool entry 不作为 HTTP view。validator 检查 boundary shape、ID、authority、动作与业务模块互斥。

## 共享接缝修订及实测红例

`actionState` 对 display 的 dependency 只核对模块声明上界；display 本身必须明确 allow，不能由 read 推导。实际 `surface.data_rule` 与固定 entry 仍要求当前对象 allow。这样 show_submit 的 precheck 配置依赖不会要求 upload 携带另一次 precheck 对象票。缺 dependency、缺 field、正式 submit deny/仅 requires_context、跨源借 show 的反例均拒绝。

`useContextTransport` 增可选 generated Rule 回调，在真实授权视图确定 source 后、正式请求前执行。它不持久保存权限，不生成第二份 source 授权。`useContextQuery` 复用该入口。普通 API 的 Axios adapter 提取到纯 `domainApi.ts`，消除 `api → domainTransport → Provider → api/authz` 静态循环；原 Provider/App 与维保命令回归因此全部恢复，未放宽原验证或改造旧 mock 来隐藏问题。

独立/作者红绿证据：

| 问题 | 原红及最终验证 |
| --- | --- |
| StrictMode import cleanup 把 live 永久置 false，prepare 后卡忙 | `/tmp/itsp-s4-import-strict-red.log` 真红；setup 恢复 live 后同一真实 Provider prepare→submit 含 StrictMode 用例绿，精确四次 view/formal 且零状态请求 |
| Chat 目标 A 上传未完成，新建 B/删除 A 后旧附件串入 | Astra `/tmp/itsp-s4-chat-target-independent-red.log` 两红；统一目标失效后原附件不可见、signal aborted、无重传断言通过，并增加实际 worker 文件结果卡片反例 |
| API 引入 React 授权层形成循环 | 首轮 full 6 files 失败（含 3 文件收集失败）；抽无状态 adapter 后原失败文件+StrictMode 共 7 files/66 tests 通过，未改原 Provider/OCC/金额断言 |
| 双 batch/job locator 静默选取 | `/tmp/itsp-s4-import-locator-red.log` 真红；修后两个目标都不请求，保留明确输入错误 |
| AntD admin preview DOM 已存在但尚不可见 | 第二全量记录 not-visible；只改等待实际可见，保留 toBeVisible、秘密不回显、精确 execute command 断言，不加 sleep/放宽 timeout |

共享 `useCapturedQuery` 另用已 ready 的真实 Provider 后挂载 StrictMode、单次 query、成功及卸载 abort 验证通过；未复现额外缺陷，因此没有改它的生产实现。

## 跨端协议证据

- `s4AgentWire.test.ts` 直接消费 Astra 真实 PG/PDP+HTTP 的 catalog、snapshot、sessions/history/file preview 与 ready/terminal；不补假 context，原 JSON 值不改。只有 TypeScript JSON literal 在 validator 输入边界明确转换类型。
- `s4ImportWire.test.ts` 直接消费 `s4-import-wire-real-http.json`：precheck/void/list/partial job 的真实 view/formal context、只报告/只状态与提交回执，另将原 content 交现有 v2 parser。文件只有后端导出的 HMAC token 做脱敏，不改变授权事实。
- 文档 14 DTO golden 另作 nullable/restricted/code 闭集测试；它们不是数据库/hash 真实性证据，真实 HTTP 文件才用于跨端验收。

## 验证结果与限制

最终同一冻结实现的本地证据：

| 检查 | 结果 / 日志 |
| --- | --- |
| 完整前端 Vitest | **187 files / 2150 tests passed**，111.78s；`/tmp/itsp-s4-frontend-full-third.log` |
| TypeScript | `npx tsc --noEmit` exit 0，`/tmp/itsp-s4-frontend-types-freeze.log`（无诊断） |
| 生产构建 | Vite build exit 0，17.69s；`/tmp/itsp-s4-frontend-build-freeze.log` |
| 生成与格式 | frontend generator `--check`、107 policies `--check`、`git diff --check` 全过 |
| 导入/控制面专项 | 5 files / 37 tests passed；`/tmp/itsp-s4-import-control-final.log` |
| 独立跨模型复审 | 5 files / 53 tests passed，Chat 独立 3 + Chat 6 + Import 9 + SSE 33 + 真实 HTTP golden 2；`/tmp/itsp-s4-frontend-cross-review-frozen.log`，结论见 `s4-astra-platform-frontend-cross-review.md` |

首轮全量 18 failed/2090 passed，另 3 文件收集失败；循环依赖与颜色扫描等问题已逐项闭合。第二轮 2146/2148，剩余两项实际 Drawer 交互/可见性同步均保留原断言修正；第三轮全部 2150 通过。完整回归继续包含原 S0/S1/S2/S3 断言。Vite 仍有现有 chunk size warning，无构建失败。

已知运行边界按冻结合同保留：Import 五来源需同组启用；canonical 列表/混合或授权视图缺必要 enabled source 返回 503，前端不落旧 handler。PDP 使 `/me` 503 时受控 UI 不可用，本轮不新增第二控制快照/故障旁路；普通 Grant 撤空但 PDP 正常时的实名管理恢复已有真实 App 测试。上述不是生产验证；最终本地 server/浏览器与用户验收由根代理统一进行。

## 文件所有权

前端 owner：`App.tsx`；`api.ts`、`api/{authz,accounts,imports,chatStream,importProtocol}.ts`；`authz/{accountAccess,contextQuery,domainTransport,domainApi,rules,generated}.ts`；`pages/{ChatPage,ImportPage}.tsx`、`pages/chat/useChatStream.ts`、`pages/import/CanonicalImportPage.tsx`；`pages/accounts/{PermissionCenterPage,UnifiedPermissionCenter,AccountProfileModal,AccountActivityDrawer}.tsx`；对应测试与显式 S1 历史 fixture 升级；`backend/scripts/generate_authz_frontend.py`。Astra 独占 `S4ChatLifecycle.independent.test.tsx`，本 owner 不修改该独立断言。未改后端领域实现/单一 manifest 与 registry。
