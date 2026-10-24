# S3 frontend implementation review

状态：前端实现与本地完整验证完成；等待跨层真实验证及阶段验收。本文不代表远端 CI、真实浏览器验收或阶段通过；不涉及生产部署。

## 单一协议与共享边界

- `generate_authz_frontend.py` 消费运行时代码 `RESOURCE_DEFINITIONS`、catalog manifest、context registry；docs 不参与运行时。生成 96 个执行资源的词表、封闭 surfaces/routes、context kinds/collections/entries/authorization views。目录同时保留 planned/retired 状态，不能把目录数量当已完成证据。
- `/me` display allow 只开放解析壳。真实数据和写入分别经已登记的当前 entry authorization view → 实际 context allow → 带单个 authorization_precondition 的正式请求。客户端不请求任意 resource/action，不把 requires_context 当业务 allow。
- `contextProtocol` 校验完整 branch requirements 的资源/动作集合、实际 carrier locator、Grant version、scope fields 上界、epoch/catalog；正式 JSON 再验当前 entry admission rule。workspace 六块允许 OR admission，完整结构仍必须覆盖六份决定，不能扁平 AND 或拼跨对象票。
- `domainTransport` 捕获 token/snapshot version/generation/input/signal。只有整个 entry 所有资源均 legacy 才使用旧 transport；混合 canonical/legacy 仍走对象协议。403/409 不重放；协议/PDP 503 失效 actor snapshot。
- `ContextSurface` 按 actor generation、snapshot version、实际对象清理局部树和 portal。路由 requires_context 只挂解析壳；无权第一帧无旧事实。

## 页面与共用组件接入

- F1：Home 的卡墙/目录/全局工作台/工作簿独立；Analytics、Dashboard 各 section、维度、drill/export 以自身 surface 和 entry 取数。orders-only 与 managed-rows-only 可进入各自壳，不借卡墙/summary 权限。
- F2：项目详情的 identity、summary、workspace、总表、overview/source assignment、parts/orders/rows/procurement、expense、collection、现场领用/返还、acceptance 分别接入；主页面不一次加载所有 tab。D03 managed/general 选择同项目一份完整合同，fields/scope 不并集。
- 负责人维护读取独立 `maintenance_project.read` 正式 DTO 的真实 `manager_assignment` 和 OCC。manager surface 可进入项目深链；缺 project.read 明确显示依赖并零写，不将缺失关系当 null。该 UI 组合依赖不成为后端写动作的隐式 read 要求。
- 现场领用、单条/批量返还、导入任务、审计、库存/回收/坏件/变卖独立门；关闭作废或审计窗口会取消授权预检。AntD `confirmLoading` 会阻断关闭，因此作废窗口仅确认按钮 loading，取消仍推进 intent lifetime。
- 多页候选/领用/需求单拼接核对同一 authorization_context；跨关系 revision 拒绝拼接。快速预览新领用单会取消旧目标解析。
- `FrontStockPanel` 数量不依赖成本字段；即使 DTO 错误携带成本，缺自身 C 的金额列/合计也不显示。WorkbookRoundTrip 支持 validate-only，结果可审阅而 apply 按钮不可用。
- F3 需求单/补库与 collection/acceptance、四个未挂载金融组件由 root 实施；补库输入生命周期修订由 frontend 接手：hydration read 仅绑定项目，autosave/flush/submit/revise 的 command transport 绑定完整 cart input，输入变化终止旧 view，不在正式写后才检查。

## 本地验证与发现

- 首轮全量：178 文件，2032 passed / 35 failed，8 个 unhandled errors；日志 `/tmp/itsp-s3-frontend-full-first.log`。失败来源已定位：新 manifest 缺 legacy_sources、旧页面 fixture 未声明 legacy snapshot、trailing capture 参数、历史目录数量和已减少的颜色配额、新增测试的精确动作/AntD 可见性。
- 已修真实目录问题：73 个维护 descriptor 从旧候选目录复用 legacy_sources；generator 与浏览器 catalog parser 均强制 string[]，完整真实 manifest 测试含缺字段毒针。未加 UI fallback。
- 旧 fixture 只增加显式会话/legacy 模式、captured transport 参数或恢复 factory mock 的固定异步响应；原数量/筛选/导出/SN/版本断言保留。批量返还旧账号成功回执断言收紧为跨账号零消息、零后续写。
- 最新共享/context、项目、返件、工作簿组合：4 文件 71 passed，`/tmp/itsp-s3-real-context-final.log`。覆盖实际 Provider、无隐式数据/read、context 不一致、关闭迟到写、缺字段 poison DTO、validate-only。
- 首次生产 build 通过，`/tmp/itsp-s3-frontend-build.log`；只含既有大 chunk 提示。最终生产 build 也通过，`/tmp/itsp-s3-frontend-build-final.log`，仅既有 chunk 提示。
- 补库 autosave input 反例确切红：`/tmp/itsp-s3-cart-input-red-exact.log`，旧 transport 在改 input 后 signal 未 abort；修后 canonical 10 + legacy 17 = 27 passed，日志 `/tmp/itsp-s3-cart-input-final.log`；同一组合含仅 submit、无 cart_update 的已保存草稿提交。

- 金融四组件跨作者复审：5 文件 36 passed，`/tmp/itsp-s3-orphan-financial-cross.log`。发现并修复 raw reminder DTO 直接显示：canonical 必须传选中 projectId 与真实 query BoundAuthorizationContext，完整校验 actual locator、capture、正式 read allow；新增 missing/stale/wrong-project/deny 四毒针。原金额遮蔽正例保留。FollowUp 的生命周期同时兼容 StrictMode setup/cleanup。
- 运行时生成器 `--check`、tsc 与 diff whitespace 检查通过。API 消费 AST 检查维护页面/共用组件未发现裸领域请求（排除纯序列化 helper 与显式 categoryRead transport）。

- 最终完整前端：179 文件 / 2081 tests 全通过，无 unhandled errors；`/tmp/itsp-s3-frontend-full-second.log`。该完整运行包含 root 冻结金融包、raw DTO context 修复、补库 input 竞态修复与所有此前 S0/S1/S2 回归。
- 静态目录复核：73 维护/补库执行资源、106 surfaces、6 routes；43 route-entry surfaces，13 无路由 dormant surfaces。导航计数不等于 E2E验收计数。

## 尚需证据

本地完整 frontend/tsc/build、generator --check 已通过。共享协议/领域剩余跨作者复审、真实 API/browser、远端 exact CI 尚需汇总。106 surface 引用闭合不是 106 组独立端到端验证；无实际挂载的 dormant surface 保持无路由入口，不能声称已人工逐一验收。


## Collection evidence selector 追加对拍

按最终服务端 registry 再生词表，evidence upload 仍仅发送真实 multipart File，file_sha256/filename/mime_type 由后端实际输入归一并绑定 revision，前端不自行传可信 hash。新增真实 Provider 3 个反例：同一 File 二阶段携同 carrier precondition；正式 409 不重放；同名同大小但不同 File 替换会 abort 授权视图，迟到 allow 不发送正式上传。shared context 20 + Collection 4，共 24 passed（`/tmp/itsp-s3-context-collection-file-binding-final.log`）；tsc 通过（`/tmp/itsp-s3-context-collection-tsc.log`）。未改金额或业务页面实现。

## 最新完整回归（领域 registry 收口后）

需求单、原件工作簿与各 import/restore 的最终 runtime selectors 再生后，完整 frontend **179 文件 / 2084 tests 全通过**，无 unhandled errors：`/tmp/itsp-s3-frontend-latest-full.log`（119.60s）。最新 tsc 通过：`/tmp/itsp-s3-frontend-latest-tsc.log`；最新生产 build 通过：`/tmp/itsp-s3-frontend-latest-build.log`（Vite 17.48s，只有既有 chunk 体积提示）。generator `--check` 和全树 `git diff --check` 同时通过。本段替代先前 2081 tests 作为最新本地完整证据，仍不代替远端 exact CI / 真实浏览器。

## 实际 main / 前端响应最终接缝

补充真实主应用接入检查见 `s3-frontend-protocol-integration-review.md`。已修旧 Beta 白名单挡 canonical APIs，以及登录/实时 beta-features 旧页面布尔挡补库新 Grant 导航。实际 App 与 Demand 组合 14 passed；33 组真实 PostgreSQL/Cerbos HTTP 响应直接通过当前前端完整 snapshot/context/payload validators。最终主应用、旧 Beta 与 batch 独立组合 52 passed。最新 tsc/build（8.29s）、generator --check、diff check 通过；当前 app/tests 作者冻结，最后远端 exact CI 尚待根代理统一执行。
