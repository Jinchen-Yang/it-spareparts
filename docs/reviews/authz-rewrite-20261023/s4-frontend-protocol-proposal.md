# S4 前端协议接缝（历史设计冻结）

实施与最终本地验证见 [S4 前端实施记录](s4-frontend-implementation-review.md)。下文保留开工前设计状态，不作为当前完成证据。

设计阶段只核对真实消费者并冻结已获 root 裁定的消费接缝；未修改 S4 app/tests，未把文档登记为 executable。S3 精确 CI 阶段门通过并获 root 开工通知后，才实施本文件。资源、动作、context 和 surface 的最终权威仍为后端 executable definitions / manifest / registry，前端生成器只消费这些输入。

真实页面仍只有 `/chat`、`/import`。Agent 有 13 个 HTTP 入口（agent.py 5、chat_sessions.py 8）；通用导入有 9 个。没有文件中心、聊天历史、作业管理的独立路由。先前入口盘点见 [S4 差距清单](s4-frontend-readonly-gap-list.md)，S2 已有保护见 [Agent 来源实施记录](s2-agent-lineage-implementation.md)。

## 1. 最小共享接缝

- 继续使用唯一 `AuthzProvider`、generated vocabulary、surface `show AND data_rule`、captured runner。`/me.requires_context` 只允许解析壳；对象/命令必须按后端登记的真实 entry 解析。无独立 Chat/Import 权限缓存或手写资源白名单。
- 普通 JSON/Blob 沿 `useContextTransport` / `domainApi`；SSE 需要 fetch adapter，但复用相同 capture、input lifetime、context validator 和 precondition。不能让 SSE helper 在 await 后重新读取 localStorage 身份。
- 每次请求捕获 `{token, snapshotVersion, generation, signal}`；业务寿命另含 `{sessionId/fileId/jobId/batchId, inputRevision}`。query key、首帧投影和异步 commit 都核对该归属。modal close、切目标、修改文件/命令必须使原意图失效。
- 来源授权继续由 S2 的 AgentAccess / provenance 完整复验；新增模块动作接入同一服务端授权链。执行许可与持久内容来源分开复验：use/run 不能机械写入历史来源，迫使 read-only 历史再获得 run。前端既不从 tool name 推授权，也不把 `file.download` 或 `session.read` 当作来源权限的替代物。
- 从真实后端 context registry 生成新的 closed kinds/views；没有登记的 entry 不猜授权视图。S3 单 carrier、正式读复验、写入不自动重放原则继续适用。

## 2. Chat 的实际接入与 surface 建议

动作组合已按 root/Astra 裁定对齐；精确 display descriptors 与 surface Rule 位于 [44 面 manifest 片段](s4-frontend-manifest-surfaces-fragment.json)。本文件不另列可执行词表。`route_entry` 只决定入口，不能授数据；现 planned sessions/files 整包 AND 被独立面替代。

| 建议 surface / 位置 | 实际数据或命令 | 入口与组合要求 |
| --- | --- | --- |
| `private_chat_session.list`，历史栏与移动 Drawer | GET `/agent/sessions` | true；独立列表读，不附送 create/run；与正文共用 read 动作，展示门独立 |
| `private_chat_session.messages`，历史正文 | GET `/{session_id}/messages` | target entry；真实 owner + 当前全部来源，切会话第一帧清旧正文/trace/文件 portal |
| `private_chat_session.create`，新对话/首次发言 | POST `/agent/sessions` | true；创建成功不附送 run。可提供本地空白草稿，但不能暗发 create |
| `private_chat_session.run`，发送与续问 | POST `/{id}/chat/stream` | true；固定 `agent_chat.use AND private_chat_session.run`；不把历史 read 动作绑定为运行前置，但实际使用的历史来源仍复验 |
| `private_chat_session.attach`，返回进行中会话 | POST `/{id}/chat/attach` | target entry；独立 attach，不要求 use/run；generating 标记不是许可 |
| `private_chat_session.cancel` / `.delete`，停止/删除 | POST `/{id}/chat/cancel` / DELETE `/{id}` | target entry；逐命令解析，不要求读取已失效来源才能删除/停止，也不回传正文。删除拒绝不能仍从本地列表移除；停止失败不能伪装服务端已停止 |
| `agent_file.upload`，附件输入 | POST `/agent/upload` | embedded；上传完成只绑定原草稿意图，不自动附到已切换的会话 |
| `agent_file.preview`，FileCard Modal | GET `/agent/files/{id}/preview` | embedded；只允许预览的账号不得自动 GET 原图片文件 |
| `agent_file.download`，下载/图片原件 | GET `/agent/files/{id}` | embedded；独立当前许可；Blob 返回后由当前 capture commit 创建 URL/点击下载 |

`rename` 有 API/helper 但当前无 UI，不为覆盖计数添加按钮。无状态 `/agent/chat` 和 `/agent/chat/stream` 由后端闭合；canonical 请求仍只接受 user 输入，不能因新流协议恢复客户端 assistant 历史。文件当前 S2 canonical 是 stable owner 本人且完整 provenance；planned `self_or_explicit_file_admin` 不能据字面实现跨 owner 管理员读取。

ChatPage 的局部重构边界：页面编排 + session controller + FileCard + SSE parser。它们共享 capture，独立保存显示状态，不保存第二份授权事实。会话切走只 detach 本地流，保持既有后台 worker 续跑语义；只有显式 Stop 才发 cancel。

## 3. SSE 唯一 wire 与定位壳

SSE 字段、错误联合闭集、sequence 和终止语义唯一权威是双方对拍的 [Agent SSE wire v1](s4-agent-sse-wire-contract.md) §1–4；本文件原候选 shape 已被替代，不再复制第二张表。ready 先于任何内容，完整 attach 从 seq1，缺段零内容，EOF 无 terminal 失败，所有 409 自动重发删除。动态来源仍服务端逐边界复验；执行 use/run 不机械记入持久内容来源，checkpoint 增长不改变 owner/固定命令 revision。

root 批准最小已知 ID 定位：`/chat?session_id=<id>`、`/import?batch_id=<id>`、`/import?job_id=<id>`。只有相应 surface 可解析时展示定位壳；未知/非法 ID 不发业务请求，互斥 locator 同时出现应显示输入错误，不猜优先级。缺 ID 可显示输入定位，实际 target 授权解析成功后才能取内容或发命令。定位不能自动发 list/read/create/run，job 也仅在用户明确打开获准的状态面后轮询。目标存在但无权不可显示从旧缓存留下的标题、计数或内容。

已知会话支持 history-only、attach-only、cancel-only、delete-only 独立到达。无 session ID 的发送需要另获 create 才可懒建；不能由 use/run 推导创建。文件三面保留 embedded，不增加文件中心。断本地连接不自动 cancel 后台运行；用户停止必须按 cancel 独立命令处理。

## 4. Import 的实际内容与授权协议分层

五来源已裁定为 `import_purchase/import_sales/import_inventory/import_maintenance/import_expense`，另有独立 `import_expense_void`。workbook 是选择包装，实际执行 expense，不是第六源；inquiry 没有执行路径。业务语义见 [Import 执行合同](s4-import-execution-contract.md)，精确 DTO 唯一表为 [Import wire v1](s4-import-wire-contract.md)，本节只说明消费者边界。

| 真实 surface | 对应合同建议 | 关键独立性 |
| --- | --- | --- |
| 文件选择/预检 | 各实际 source 的 precheck + show_upload | 路由入口 OR 各可用 source；一个文件的真实域由服务器解析，文件名不能授域 |
| skip/upsert 提交 | precheck AND (submit_skip OR submit_upsert) + show_submit | show 的预检准备依赖已批准，正式 mutation API 动作自身独立。多文件实际提交须满足全部执行域/对象；回执不自动授 job 读 |
| 报销作废预演 | import_expense_void.preview + show_preview | 实际读取旧报销身份、金额及合同；不能借纯内容 precheck。保留现有 HMAC preview_token 与授权 precondition 两种不同约束 |
| 批次历史 | read_status + show_history | 独立 mount/query；原全库历史不能擅改为 owner-only |
| 作业进度 | 各真实 source 的 read_status + show_jobs | 无 submit 也能读获准已有 job；submit-only 不自动轮询。多源 job 按实际完整 manifest 投影 |
| 单批报告 | read_report + show_report（同资源 C/S/K/F） | 不附送 error_export；report 中原始错误/来源号/金额不能默认都是无敏感 metadata |
| 问题 CSV | export_errors + show_errors（同资源 C/S/K/F） | 独立 Blob 消费且当前 capture 才保存 |

内容预检 v2 原 DTO 保持：`can_import_all/can_import/severity/selected_sheets` 由已有 parser 按内容重算，权限 deny 不能写成内容 error 或篡改 can_import。在外层增加版本化授权信息，内容 parser 只接原 payload；最终是否可提交是“内容可提交 AND 当前完整执行合同”，不是修改内容事实。现裸数组 `/import/batches` 若转 `{items,...}`，必须显式 adapter 版本，不能悄悄尝试两种 shape。

formal precheck 只有实际 selected 各源全部准入才返回原内容；缺任一整体 403，授权视图仍只有标准 locator/context。precheck context 不能当 upload precondition，正式提交必须走自己的同 entry 授权视图。文件按不可变 `file_index` 配对，同名文件不能作为 identity。mixed job 仅获准 items，partial 时顶层 counts/status/note 与 item.input_index 为 null，并有 restricted=true；允许空结果才是 0。只按 visible 项计算可见进度，不能推断隐藏域数量或把受限作业显示为未开始。

现有 expense preview_token 继续绑定实际 bytes、合同、作废指纹；新 authorization_precondition 绑定本次 actual command plan 与授权观察，两者都随文件顺序、mode、inputRevision 失效。正式提交仍一次人工意图，409 不自动重放。job 轮询遇授权拒绝或 503 立即停并清相关投影，不继续当前 1.5 秒循环直到 15 分钟超时。测试名称使用“可预检/提交但无状态/报告”，避免把 UI 必要预检和单 API submit 权限混为一谈。

消费者适配集中于 `api/imports.ts`：precheck 的 content 交旧 v2 parser；void 的 previews 逐项交旧 parser，并核对请求数量/顺序；batch 的 metadata/report 各自按固定 block_access 显示，不将 metadata=null 展平成空字段或 0；job 读取 response.job，partial 时按 visible_status 判断可见项是否继续轮询，文案不得称整作业已完成；提交 accepted_files 只作为本次已接收回执，不伪造已读 JobProjection。旧 raw array/total_files/batches 形状仅由明确 legacy adapter 使用。直接报告按 batch_id 与 source_domain 定位，metadata 拒绝时不依赖原 file_type 才显示获准报告。

只读对拍证据：14 个 golden DTO JSON 可解析；通过内存转译现有 `normalizeImportPrecheck` / `normalizeExpenseVoidPreview`，其中未识别输入样例仍为 v2/blocked，K=false 预演仍为 ready 且 amount=null。没有修改 parser、没有新增 app/tests；这两项形状检查不代替实际授权或业务测试。

## 5. 可执行分工与验收矩阵

前端 owner 统一负责 `api.ts` 的 Agent helpers、`api/imports.ts`、ChatPage、ImportPage/`pages/import/*`、generated/nav/App 接入；可拆小型 transport/parser/UI 文件。Astra 负责 Agent/SSE/文件后端及同一 provenance 扩展，backend agent 负责五域 import；root 负责 executable 目录/控制面/机器边界。生成器仍由前端 owner 接最终 registry，不能读取本 Markdown 运行。

| 包 | 必须新增的真实边界验证 |
| --- | --- |
| 共享 / route | unknown schema/action/context 关闭；history-only 能入且零 create/run；新账号无 implicit show；只上传/预览的 embedded surface 不意外打开另一个页面 |
| SSE parser | UTF-8 分块/CRLF/multiline；坏 JSON/未知事件/错序/EOF 无 done；callback 抛错不吞；5000 上限 attach 缺段；授权 error 不保留 partial；busy 与授权 409 不混淆 |
| 会话 | A→B→A 首帧无旧 turns；旧 stream/title/tool/401 不污染新 actor；create 成功但 run 拒；停止失败、删除拒绝可见；detach 不误 cancel；来源撤权后 history/attach 同拒 |
| 文件 | preview-only 不请求原图片；晚 upload 不附新草稿；Modal close/撤权/换文件 abort；晚 Blob 不 click；URL 回收；stable owner rename 不失权，另一 admin 不自动读取；原来源撤权后文件同拒 |
| Import | 保留全部内容 parser/作废 token 断言；同名文件位置绑定；mixed source 仅一个允许不能提交整批；history-only 零 precheck，可预检/提交但无状态/报告时零 poll；变 mode/bytes 后迟到授权零正式提交 |
| 历史/作业 | batch/job A→B→A；异域隐藏计数不是 0；503 不假成功或后台持续轮询；report-only 不导出；CSV 迟到不保存；不确定提交结果不自动重复写 |
| 真协议对拍 | 用真实后端 JSON/SSE 样本喂前端 parser/context validator；成功 context 与生成 entry 完整分支一致；不能只 mock useAuthz 或只测菜单隐藏 |

SSE §1–4 已双方对拍；Import 精确 JSON 与平台 boundaries descriptor 以各后端 owner 的唯一合同为准。文件内容绑定的实际 sidecar/迁移细节仍由 root/Astra 收口，不由前端发明。上述矩阵是待实施验收，不是通过记录。

## 6. 权限中心与平台目录消费

以 [root 平台边界合同](s4-platform-and-context-contract.md) 为准：普通 `account_directory.read/read_activity`、`account_profile.update` 进入模块；其余实名管理/bootstrap/public/servicecredential 条目移到 catalog.boundaries，不能当 employee Grant。modules 仍用现枚举；boundaries 固定 `{id,label,principal_kind,authority,actions:[{id,label,kind}],scope,notice}`，不含 migration_state/configurable/Grant 字段。前端生成独立 KNOWN_BOUNDARIES，严格校验真实 ID、固定 authority/动作及 shape，不能靠缺字段回退旧协议，也不能从 boundary 元数据推导 allow。平台说明可小型只读展示，不增加角色推断。

已核真实接入点：`App.tsx` accounts 路由和 `PermissionCenterPage` 顶层 read_accounts 总门须同时改为 actor 管理能力 OR 普通 surface；`UnifiedPermissionCenter.load` 不能无条件请求 `/accounts`。授权管理员使用 `GET /authz/accounts` 最小目标发现，普通目录只在本资源实际运行状态下获准后请求。模板列表仍按 actor 管理能力，不借普通目录 Grant。最小目标类型单独定义，不能继承带 permissions/template_perms/overrides 的旧 Account。enabled 资源的 actor_capabilities.read_accounts/edit_account_profiles 只表达数据/命令许可，普通路由、load、render 和按钮仍须 generated show AND data；管理员也不能凭 manage_authorization 获得资料更新或活动。安全属性仍独立走原管理 capability。

管理模式与普通资源切换不可合并推断。即使 S1 已使 `canonical_management_required=true`，普通目录/资料仍可能从未切换，按服务端 runtime 逐资源选择：

| 普通资源状态 | 普通面与请求 | 管理恢复入口 |
| --- | --- | --- |
| `legacy`，且服务端确认从未 cutover | 使用服务端按实时旧能力派生的 actor capability 作为明确 legacy 展示/执行分支；不读 localStorage 旧权限或自行按角色放行 | 若管理 capability 允许，独立使用最小目标发现和规范模板，不要求普通目录读 |
| `enabled` | generated `show AND data_rule`；命令按对应 Grant；actor capability 不能补缺失 show | 相同独立管理入口；不由该入口授普通资料/活动 |
| `suspended` | 拒绝且清旧数据；不得回到 legacy 分支 | 仅真实管理 capability 可用，普通面仍拒绝 |
| 未取得快照、未知状态/schema、`/me` 故障 | 加载壳或不可用；不读取缓存能力执行请求 | 本轮不加第二 control snapshot/cache 或故障旁路 UI |

canonical 页面不调用 `/accounts/_meta`：该调用仅在 `LegacyPermissionCenterPage.load`。因此管理切换后此旧 meta 返回 410 不阻断新页面；未切换管理的 legacy 页面仍消费原 DTO。canonical `/accounts` 与 `/role-templates` 改为精确 DTO 时同步收紧类型，不要求旧 permissions/template blob。PDP 故障使 `/me` 整体 503、受控 UI 不可用；实名管理 API 本身仍独立可用，不把这个 API 边界宣称为故障下的 UI 恢复能力。

活动面目前只有 legacy AccountsTab，统一页需要恢复该真实面但独立 show_activity/read_activity，绝不把最小目标发现返回值当活动内容。display_name 资料命令仍与角色/销售映射/模板/安全命令分离；安全/授权命令保留 S1 CAS、预览、原子提交与本人撤权清理。profile-only PUT 按 S1 wire §6 仅发送 schema/catalog/profile.display_name，不需要 expected_epoch，不推进授权 epoch，也不声称资料 CAS。新反例：管理员无普通目录 Grant 仍可管理授权且零 `/accounts`/activity 请求；普通目录账号零管理目标发现；目录读不附送活动；资料 update 不改安全字段。

root 已冻结独立目标入口 `/accounts?username=<准确用户名>`，缺参数时可输入用户名打开；不为补齐目标数据调用目录发现。`account_directory.activity` 与 `account_profile.edit` 的 `route_entry=true`、`page_routes=["/accounts"]`，规则仍分别是 `show_activity AND read_activity`、`show_edit AND update`，不增加 directory.read 依赖。这两项允许独立菜单/路由壳；实际用户请求的目标未获准前不显示缓存资料或活动。点击一个面只调用其自身接口，不自动打开另一个面。

profile-only 壳可以用实际 username 与用户明确输入的 display_name 发既定 profile body，无 epoch，也不增加元数据接口。不能为了复用原 `AccountCommandModal` 而伪造完整 Account 行；可拆小表单复用现有 runner。未读取当前显示名时准确提示，不把空输入当成已知当前为空；清空显示名须由用户显式保存。换 username、关闭或输入改变使捕获的旧命令失效。新增反例：activity-only/profile-only 已知目标各自可达且零目录请求；缺 ID 只有输入壳；资料请求不带安全字段/epoch；另一目标迟到结果不污染当前目标。

管理目标 finder 与普通目录共用的授权摘要投影新增 `authorization_summary_status: "available" | "invalid"`，`authorization_summary` 为原封闭对象或 null：available 必须有合法对象，invalid 必须为 null。正常的 invalid 单行显示“授权摘要不可用”，保留其已获准的最小身份与管理定位；不能将 null 当空 Grant/无模板，也不能阻断其他正常行加载。实际权限编辑/修复仍走目标诊断与当前管理命令，不从摘要推断授权。未知状态或矛盾 shape 仍按协议错误处理。两种目录均不含 `last_login_at`，仅独立获准的 activity 响应提供最近登录/活动信息；目录缺字段不能用旧登录数据补齐。

新增兼容反例必须包含“仅 sales 已切换、目录/资料从未切换”仍保持实时旧能力，以及“已有 marker 后停用”拒绝而不恢复旧允许；摘要反例包含一条 invalid 与正常目标并列、恢复管理员仍可选定目标、无活动权时不请求或显示 last_login_at。
