# S4 Agent SSE wire 合同 v1（设计冻结，实施等待阶段门）

本文件由 Agent 后端与前端消费者共同对拍，为 SSE 唯一字段表。root 已批准 13 入口动作、self 文件范围、执行许可与持久内容来源分离；应用/测试尚未实施。HTTP 入口及动作见 [Agent 边界](s4-agent-boundary-readonly.md)，前端不另维护第二版事件 schema。

## 1. HTTP 与握手

沿现有 POST `/api/agent/chat/stream`、`/api/agent/sessions/{session_id}/chat/stream`、`.../chat/attach`。JSON 请求分别为现有 user-only messages、`{message}`、无业务 body，并通过同 entry 的授权视图取得当前 `authorization_precondition`；前置条件仅新增到正式请求，不进入业务输入摘要。stream view 必须零写、零 LLM、零 run 创建。正式请求在开始发送 HTTP body 前完成真实身份、owner、完整动作、当前历史来源和前置条件校验。失败使用既有结构化 HTTP 错误，不能先发 ready 再报告根本未接受的命令。

`Content-Type: text/event-stream`，`Cache-Control: no-store`，`X-Accel-Buffering: no`。不使用 SSE id/retry 或 Last-Event-ID 部分续传。每个事件为 UTF-8 SSE `data:` 的 JSON；允许标准 comments、LF/CRLF 和 multiline data，解码器必须正确处理拆分的 UTF-8 字符与 EOF flush。单个事件合并 data 的 UTF-8 上限为 1 MiB，不能把包含多帧的网络 chunk 当单帧限额。无终止帧的 EOF 属于传输中断，不是成功。

第一帧必须且只能包含以下六个字段：

```ts
type Ready = {
  type: "ready";
  schema_version: 1;
  session_id: number | null;
  run_id: string | null;
  authorization_context: AuthorizationContext;
  replay_complete: boolean;
};
```

- `session_id` 为真实正安全整数；无状态 chat 为 null。会话请求必须与捕获的 path ID 一致。
- `run_id` 是服务端为实际运行分配的 32 位小写 hex UUID，不是权限凭据。即使 provider 未配置而返回说明文本，也分配实际 run ID。只有 attach 确无活跃运行时为 null。
- `authorization_context` 原样使用共享 schema1（catalog_version、authorization_epoch、context、resources），来自本次正式 entry 的当前 `ContextAccessSet.wire(actual_ref)`；不发送另一份 allowed 或角色列表。前端按 captured snapshot 与生成的 exact entry 验证。attach 的 context 是当前 attach 合同，不复用运行发起者的 run/use context。
- 该 context 只代表本次命令/读取的真实 owner、稳定生命周期和固定输入观察；不纳入此轮 checkpoint updated_at、正文长度、message count。业务来源动态增长始终由服务端 collector 检查，不用新 ready 或前端来源数组“续签”。
- `replay_complete=true` 表示此连接将获得从 seq1 开始的完整事件前缀及后续流。为 false 时只允许随后一帧 CHAT_REPLAY_GAP error，禁止任何 title/正文/tool 内容。

## 2. 事件闭集

除 ready 外，每帧必含 `type/run_id/seq`。`run_id` 必须等于 ready；`seq` 为安全整数，从 1 连续递增，不能重复、跳号或回退。拒绝任何未列字段。`args` 是递归合法 JSON 对象（不能 NaN/Infinity/非对象），工具调用记录严格为 `{name,args}`。

| type | 额外必填字段 | 可选字段 | 终止 |
| --- | --- | --- | --- |
| delta | text:string | 无 | 否 |
| thinking | text:string | 无 | 否 |
| tool | name:string, args:JSON object | 无 | 否 |
| tool_done | name:string, ok:boolean | 无 | 否 |
| title | title:string | 无 | 否 |
| done | tool_calls:ToolCall[], stopped:boolean, configured:boolean | answer:string | 是 |
| error | code:封闭码, category:对应分类, message:string | 无 | 是 |
| no_active | 无 | 无 | 是 |

`done.answer` 保留既有“最终 provider 轮内容”的语义，不保证等于多轮所有 delta 拼接，前端不得再次 append。展示正文以本流 delta 为准，持久历史仍从获准的历史入口读取。`configured=false` 的说明流程仍为 ready → delta → done；`stopped=true` 明确用户停止，不冒充授权失败。

`no_active` 仅允许 attach、ready.run_id=null、replay_complete=true 的分支，seq=1。该 ready 后如果实时身份/权限发生错误，也允许 run_id=null 的唯一 error(seq=1)；不能凭空生成一个运行 ID。其他分支 run_id 均非 null。

终止后不再允许任何 data 事件。解码和业务 callback 异常必须向上抛出并中止，不可跳坏帧或吞掉异常继续显示。

## 3. RunHub 顺序与 attach

run ID 和 seq 在真实 RunHub 中生成，`title` 也进入同一 hub 顺序，不能由每个 subscriber 单独注入。生产者 publish 在同一 hub 锁下分配 seq、追加 buffer 并扇出；subscribe 在同一锁下取得 buffer 与完整性标记后注册队列，避免 replay/live 间隙。

每次 attach 都完整从 seq1 回放，前端清本轮旧 buffer 后消费；不拼接既有 partial，不接受 offset/Last-Event-ID。当前 5000 事件上限保持：达到上限后的第一次无法缓冲使完整性永久为 false，已连接 live subscriber 可继续接收连续事件；晚 attach 必须 ready(false) → error(CHAT_REPLAY_GAP, seq1) 并关闭，零内容回放。前端提示重新读取获准的持久历史；无 read 权时不可自动补授或暗发历史请求。

每个 consumer 发布事件前复验当前请求的模块许可与同一运行的累计来源；已排队不表示可发布。若某 consumer 的授权失效，在该连接生成 expected-next seq 的 terminal error，不写入共享 run 的历史顺序；不会给另一连接凭空插入/漏掉事件。worker 本身授权失效则停止网络/工具/持久化发布，统一终止其 hub。

## 4. 错误码与分类（联合闭集）

```ts
type StreamErrorCode =
  | "AUTHN_SESSION_INVALID"
  | "AUTHZ_FORBIDDEN"
  | "AUTHZ_CONTEXT_STALE"
  | "AUTHZ_ARTIFACT_STALE"
  | "AUTHZ_RESOURCE_UNAVAILABLE"
  | "AUTHZ_ENGINE_UNAVAILABLE"
  | "CHAT_PERSISTENCE_FAILED"
  | "CHAT_PROVIDER_UNAVAILABLE"
  | "CHAT_REPLAY_GAP"
  | "CHAT_STREAM_PROTOCOL_ERROR";
```

| category | 允许 code | 前端动作 |
| --- | --- | --- |
| authorization | AUTHN_SESSION_INVALID、AUTHZ_FORBIDDEN、AUTHZ_CONTEXT_STALE、AUTHZ_ARTIFACT_STALE | 停流，清本次 partial/thinking/trace/文件 portal，不重放命令 |
| server | AUTHZ_RESOURCE_UNAVAILABLE、AUTHZ_ENGINE_UNAVAILABLE、CHAT_PERSISTENCE_FAILED | 授权或存储无法可靠完成，失败关闭并清本次敏感投影，不归为 provider 错误、不自动重试 |
| provider | CHAT_PROVIDER_UNAVAILABLE | 终止本轮；不把 partial 自动升级为成功历史，可由显式且获准的历史读取取得服务端保存的 stopped 记录 |
| protocol | CHAT_REPLAY_GAP、CHAT_STREAM_PROTOCOL_ERROR | 拒绝/清本轮不完整投影，提示重新读取，不自动提交原消息 |

服务器内部未配置/PDP/DB 等异常只映射到上述稳定类别，原异常细节仅服务端日志；message 不含 SQL、内部路径或来源数据。映射不是把授权故障变成普通空回答。网络断连和本地 parser/callback 错误属于客户端 transport/protocol 状态，不伪造服务端 error 帧。旧主体的错误不得清除新主体登录状态。

HTTP busy 固定为 409：

```json
{"detail":{"code":"CHAT_RUN_BUSY","message":"该会话上一轮回答仍在生成","accepted":false}}
```

所有 409 自动重发删除。用户可以显式 Stop/Attach/重新发送新意图，但必须再次捕获同主体和当前输入、重新授权；其他授权 409 不转换为 busy。收到 ready 代表运行已接受，之后连接断开不能自动重发消息；detach 与取消后台运行保持两种操作。

## 5. 共享目录、registry、迁移交付清单

### Catalog / policies（root）

- agent_chat：use（mutation）+ show_conversation；self 范围，空字段。session stream 显式 AND private_chat_session.run。
- private_chat_session：read/attach（data），create/rename/delete/run/cancel（mutation）；self，空字段。其独立展示面及精确 show 由前端 manifest 生成，不以一个 sessions 面 AND 全部命令。
- agent_file：preview/download（data），upload/generate（mutation，generate 为工具内部动作，无 UI 面）；self，空字段；分别 show_upload/show_preview/show_download。不继承旧 admin/boss 跨 owner 例外。
- 文件工具 inspect_file/read_file_rows/read_document 均要求 preview AND 实际来源；write_excel/write_report 要求独立 generate，若使用基文件另需基文件 preview。use/run/upload 均不自动授 generate，生成不自动授 preview/download；工具结果只给安全文件 ID/格式，不附敏感 bytes 或从业务内容推导的文件名/摘要。
- seed 逐入口旧实际 page_chat 解释器与实名账号资格；generate 没有额外成本/利润或角色限制（旧 write_report 直接可用，write_excel 仅基文件加实际 owner/source 约束）。只处理持久 legacy-seed-eligible 记录，保留 canonical none/空拒绝/完整 override。enable_agent/LLM 配置仍独立 feature 开关，不充当用户 Grant。模块权利与原领域来源权利不能互相推导。

### Typed context registry（root，共享唯一 domain_contexts）

- 新真实 kind `chat_session`（ID 为数据库正整数的十进制字符串）、`agent_file`（现有 12 位小写 hex file_id）；不得复用 project 默认 resolver。真实 owner 从存储重查。
- collection `private_chat_sessions`：仅 list，selector={}，SQL owner_user_id==当前稳定主体；不能将其 proof 授给具体 session 写。
- 使用既有 operation_input 机制登记 stateless chat、session create、file upload：真实 actor、固定 entry/purpose、规范请求/实际 bytes SHA；只用于相应初始动作，不能持久为消息/文件来源。
- session run 绑定真实 session 与规范 message SHA；rename 绑定规范 title SHA；read/attach/cancel/delete selector={}。run 的权限 revision 不绑定本轮自增内容。13 entry 都需 exact action/branch/selector，view=authorization 沿同方法/路径且零写零 LLM。
- file preview/download 绑定真实 file ID、owner、内容代次/实际摘要。文件位置不是权限事实。真实非 HTTP entry 固定 `TOOL agent.write_excel` / `TOOL agent.write_report`，分别由当前实际 registry 同名工具触发，要求 agent_file.generate；operation_input selector 为规范工具名/实际完整参数 SHA，write_excel 基文件消费另走实际 preview/source。registry/generator 必须显式支持 tool channel，并从 HTTP 覆盖分母排除，不伪造 HTTP 路由。
- `domain_contexts` 唯一 resolver/reference/query/document registry；maintenance 保留项目证据与兼容 re-export，只有精确 S3 entry 显式注册 fallback。未知 S4 entry 不默认解释为 project/collection。

### Migration / runtime（root；本包不新增产物表或 run 表）

- 沿既有 receipt/cutover 策略迁移三资源与 seed，祖先 receipt/counter 保持单调；任一新资源已 cutover 不得回旧 Agent 门。
- ChatSession.owner_user_id、ChatMessage.authorization_provenance 已在 S2，SSE run_id/seq 只属进程内实际 RunHub，不需要为流协议额外建运行表或回填历史 owner。
- root 已批准复用 `SysAuthorizationArtifact` 的 binding-only 模式：实际 FS domain adapter 绑定 file_id + stable owner + generation1 + bytes SHA + closed metadata SHA，不新增产物表；旧无 binding 文件拒绝并要求重新生成，不猜 username 认领。
- 现 shared helper 硬检查 authz_s3；待实施时为 ArtifactDomainAdapter 增末尾默认 protocol=authz_s3，Agent adapter 固定 authz_s4，register 仅按服务器已登记 adapter.protocol 检查 marker。begin_artifact_protocol 保留 authz_s3 默认并接闭集 server-only protocol 参数；旧 S3 调用与语义不变。
- `mark_artifact_protocol_used(bind, protocol="authz_s4")` 在任何新文件发布及领域/用户锁之前独立短事务提交；runtime 永久权威下界识别该标记，S4 downgrade 锁后检查并拒绝移除使用中的协议。由单一迁移/runtime owner 增加此固定协议，无第二 marker 表。

## 6. 实际内容与执行许可的分离

现 S2 agent_files meta.schema_version1 只保存 stable owner/provenance，没有内容 SHA；读取只验证文件存在。S4 的 kind/ref/revision 需要实际不可变内容绑定，不能把文件名或 owner epoch 当内容版本。root 已批准复用 S3 sidecar/实际文件 domain adapter 固定 file_id+owner+generation1+bytes SHA+closed metadata SHA，发布路径由服务端 producer 登记；旧没有绑定的文件保守拒绝并允许重新上传/生成。metadata/PDP 后才物化完整 bytes；原件替换必须拒绝。底层 loader 仅解析实际存储元数据与摘要，不调用受保护 public 文件原语，避免递归触发 AgentAccess.check。此项是已批准的下一阶段合同，不是实施证据。

历史 metadata-first 要在同一真实 RR 快照内先取 ID/role/provenance，再当前全部来源授权，随后取 content/tools；SSE 本身不可作为客户端伪造的来源记录。执行合同每生命周期边界复验，但不能机械进入持久来源导致 read/attach 暗需 use/run。测试必须覆盖这些逻辑分离以及 recorder 不遗漏领域来源。

执行合同使用明确的 recorder scope：当前 use/run、upload/generate 等执行命令在每个本次生命周期边界复验，却不机械写成持久内容来源；实际领域读取与基文件 preview 的来源必须原样记录/继承，不能用资源名通配过滤所有 callback。scope 仅围绕服务器具名执行检查，必须 finally 恢复；嵌套异常不能关闭随后领域读取的录制。

**strict epoch/catalog 历史失效保持。** 当前持久 provenance 与实时主体 epoch/catalog 不匹配即 stale；即使只撤 generate 也会递增 epoch，从而使旧产物失效。去除错误动作依赖不代表旧文件可以跨 epoch 读取，本阶段不做 rebase 或历史重新绑定。动作独立性用同 epoch 的初始化权利组合验证，撤权仍按原严格失效测试。

三项现有维保工具在 S4 绑定同一已保护服务 `maintenance_cost_access.read(db, identity, *, entry_id, params, legacy_context, view=None, precondition=None, transform=None)`：get_maintenance_projects → GET /api/maintenance/projects (`maintenance_cost.read_projects`)，get_maintenance_lines → GET /api/maintenance/lines (`maintenance_cost.read_lines`)，get_maintenance_board → GET /api/maintenance/board (`maintenance_contract_cost.read`)。工具参数只能构造该真实 entry 的闭集 selector，读取、字段/范围与历史 ref 全部沿原 facade/recorder；不得恢复旧纯计算函数 + role 判断。

文件发布补偿由 root 确认：新的 file_id 不得覆盖已有原件或 meta；使用独占创建/原子私有 candidate 发布。失败仅删除本次实际创建的 candidate，不得按碰撞 ID 删除已有他人文件。绑定的完整 bytes SHA 必须在 HTTP header/首 chunk 前验证；随后仍逐发布边界复验当前权限。binding-only 不制造 producer 的 preview/download 依赖，不需扩展 ArtifactFormat 动作 rewrite。

前端展示以 `s4-frontend-manifest-surfaces-fragment.json` 的 10 个 Chat 面为共同输入；query session_id 只定位实际目标，attach/cancel/delete-only 可达该定位壳，但不暗发 list/read/create/run。没有 ID 时只显示明确 ID 输入壳，不通过额外目录读取推测目标。文件三个面仍嵌入，无文件中心。

## 7. 现有调用层只读对拍补充（等待 S3 修订 CI）

已对照 `api/agent.py` 的 5 个与 `api/chat_sessions.py` 的 8 个真实 route、`agent/runtime.py`、`agent/tools.py`、`chat_store.py`、`agent_files.py` 及 `authz/provenance.py`。13 HTTP 与上述合同一致，没有新增业务口径待决定；下列是已批准合同的必要实现接缝，不能当成当前代码已完成。

1. **Recorder 的嵌套是替换并恢复，不是自动广播。** `service.access_recorder` 当前用 ContextVar token 在 finally 恢复；`validate_provenance` 内层 noop 是故意抑制复验反馈，不能改成全局广播导致来源自录制。Agent 所有实际业务读取与文件基件读取使用同一 collector，已有 facade 当前没有另装 recorder 吞掉外层回调。将来若局部 collector 接管真实读取，必须显式把完整来源交回外层；不能仅凭 record_count 增长推断所有来源完整。具名执行检查的抑制 scope 只包住该次检查，不能包住整个工具/文件函数；异常之后的领域读取仍须被记录。
2. **跨线程与 generator yield 不携带 ContextVar token。** 保持 `_agent_loop` 在每次真实 `next(iterator)` 的线程里安装/释放上下文，然后才 yield。worker 显式携带同一 typed identity、执行合同与 collector；HTTP 请求线程或 asyncio task 的上下文不被当成自动线程继承。独立调用 `tools.dispatch(identity=...)` 也须由服务端明确建立 stateless `agent_chat.use` 合同，或拒绝缺少合同的调用；不能仅有 generate/领域读取权就绕过 Agent 执行许可。
3. **Attach 不调用 producer 的执行许可检查。** 现 `_hub_event_stream` 先 `hub.access.check()`；S4 若直接把 use/run 放入该方法，会暗加 attach→run 依赖。worker 自己复验 use/run 与来源；consumer 复验自己的 attach 合同，并验证/继承 hub 的完整内容来源。检查来源的入口不能顺带执行 producer 合同。cancel/delete 同理不读取失效来源。没有 sources 的可信本轮内容仍有主体/epoch/catalog 绑定，零 callback 只允许具名静态工具或纯用户输入分支。
4. **文件摘要与实际消费 bytes 是同一份对象。** 现 download 返回 Path 后重新 open，vision provider 又从 Path.read_bytes；S4 不能只在此前对路径做 SHA 检查。当前 metadata/PDP 通过后，将完整实际文件复制到私有不可变 bytes/spool 并验证摘要；parser、基文件派生、vision 和下载都消费这份已验证内容。header/首 chunk 前再次执行发布检查，后续 chunks 继续当前来源复验；不让路径替换窗口绕过原件完整性。慢客户端与外部 LLM 不持数据库事务，临时副本按本次所有权清理。
5. **持久化失败不能变成 done。** 当前 worker 在 `_save(final=True)` 内先置 finalized；S4 应以实际写入结果区分“正在 finalizing/已保存/失败”，失败产生封闭 CHAT_PERSISTENCE_FAILED，不再 fall through 为 provider 错误或成功 done。来源/权限失败不执行 finally 的补保存。配置未就绪、title、cancel done 也进入唯一 hub 顺序；consumer-only 错误不污染共享 seq。
6. **历史与工具不得隐式借整业务动作。** 历史只验证实际被返回/发送的消息集合来源，metadata 与 content 在同 RR 内；文件 preview 仍验证完整文件来源。维保三个工具绑定第 6 节现有 facade；其余已有业务工具继续逐工具绑定当前实际 resource/action，不能为 Agent 默认授整模块 read 或补齐缺少 action/field。inspect/read_document/read_rows 需要实际文件 preview；生成只产生安全 file ID/格式。list_skills/get_skill 属服务器明确登记的静态内容，仍受本次 Agent 执行合同约束，不能把其他“零 callback”工具推断为同样安全。

实施时最小独立反例：嵌套 recorder 正常/抛错后外层仍收到两个不同领域来源；复验不把执行动作录入来源；线程第一次与后续 next 都有同 collector；attach-only 同 epoch 可读取有合法来源的运行而缺 use/run；独立 dispatch 无执行合同拒绝；完整 SHA 校验后替换原路径不影响已验证副本且篡改首次文件被拒绝；vision 网络前/后撤权、queue 已排队后撤权、checkpoint 写失败均不泄露/不成功终止；任意 epoch/catalog 变化仍使旧 history/file stale，不进行 rebase。这些均未实施或执行，本节只是 read-only 调用层核对。
