# S4 Agent / 私人会话 / 文件接缝（只读，待共同冻结）

S3 收口后检查现有实现，未修改 S4 应用或测试。本表区分拟定模块动作与现有 S2 来源证明；新模块动作不能替代历史内容的完整来源重新授权。最终可执行目录、seed 和 wire 须 root 与前端共同确认，不把当前 planned 元数据当运行合同。

## 实际 13 个 HTTP 入口

以下路径均以 `/api` 为前缀。已有资源名可复用，无需新增“聊天历史中心”页面。

| 入口 | 建议模块动作 | 内容与身份约束 |
| --- | --- | --- |
| POST `/agent/chat`、`/agent/chat/stream` | agent_chat.use | 新权威只接 user-only 输入；服务端工具仍调用对应领域受保护服务 |
| GET `/agent/sessions` | private_chat_session.read | SQL stable owner，列表标题与 generating 标记属于响应内容 |
| POST `/agent/sessions` | private_chat_session.create | 当前实名主体创建，不附送 run 权限 |
| PATCH `/agent/sessions/{id}` | private_chat_session.rename | 真实 session owner；只返回本次提交标题与 ID |
| DELETE `/agent/sessions/{id}` | private_chat_session.delete | 真实 session owner；不要求读取已失效的历史正文才能删除 |
| GET `/agent/sessions/{id}/messages` | private_chat_session.read | stable owner AND 全部被返回 assistant 消息的当前 provenance |
| POST `/agent/sessions/{id}/chat/stream` | agent_chat.use AND private_chat_session.run | 真实 session、服务端历史；run 不能由 create/read 推导 |
| POST `/agent/sessions/{id}/chat/attach` | private_chat_session.attach | 当前源授权下的 replay/read，不要求新发起 LLM 的 use/run；归类 data |
| POST `/agent/sessions/{id}/chat/cancel` | private_chat_session.cancel | 本人实际运行；不借取消泄露正文，不因来源失效阻止终止计算 |
| POST `/agent/upload` | agent_file.upload | 当前 stable owner；返回原件 ID 不是后续 preview/download 通行证 |
| GET `/agent/files/{id}/preview` | agent_file.preview | 本人整件来源复验；截前 N 行不是缩小源合同；归类 data |
| GET `/agent/files/{id}` | agent_file.download | 本人整件来源复验；图片展示实际用此路径，不能借 preview 自动下载；归类 data |

当前 planned `agent_file.self_or_explicit_file_admin` 与 S2 已切换实例的实际本人读取边界冲突。建议冻结 `self`，不将旧 admin/boss 兼容例外重新 seed 成跨主体新权利。真正 legacy 行为仅留在从未切换的实例；persistent cutover / artifact protocol marker 撤配置后仍不可回旧旁路。三个资源没有额外财务字段：其内容权限来自实际业务来源 Grant，字段绝不可跨资源 union。

## 必须保留的来源与生命周期

- `AgentAccess` / `AccessCollector` / `access_recorder` 仍是唯一来源机制；worker 在实际线程安装上下文，每次工具、LLM 发送、流 chunk、queue consumer、checkpoint、attach/replay、文件派生与下载发布复验。没有 callback 不代表可信空依赖。
- 运行许可与内容来源必须分别复验：`AgentAccess.check()` 除原 collector 来源外，还需服务器固定的本次 entry 执行合同（use/run 或 attach 等），不能只在 HTTP 开头验模块动作。执行合同仍走同一 PDP/context，但 use/run 不应机械存成历史内容来源，否则 read-only 历史会被迫再获 run，形成隐藏依赖。精确 recorder 安装边界由 root 核准，不能过滤任意领域来源 callback。
- 继续使用 stable principal ID、epoch、catalog、resource/action/version 与 schema2 实际 object_ref/entry/branch/revision；不另造由客户端填写的来源数组。不凭 username 恢复旧 NULL lineage。
- 不持数据库事务/用户锁跨 LLM 网络、等待队列或慢客户端下载；短事务完成授权观察与数据物化。
- 现 `chat_store.list_messages/history_for_llm` 先选完整 ChatMessage 再 inherit provenance。S4 应先只取实际 message ID/role/来源元数据，在一致快照内完成全部当前授权，再读取正文/tools；这一读取顺序尚未实施或用新反例验证。
- session delete/cancel/rename 的动作与内容读取分开；只输出最小命令结果，不能为了 allow 清理操作附带历史内容。旧未知历史可删除/重新创建，不能猜授读取。

## SSE 与前端共同冻结项

保留 session 请求 `{message}` 和服务端历史；stateless 的 user-only 限制不变。当前流没有 schema 握手与事件序号，错误有 `AUTHZ_ARTIFACT_STALE`，忙冲突仍为字符串 409。建议新增统一版本握手、run ID、事件单调序号、捕获主体 epoch/catalog（主体 ID 若传输，仅用于一致性检查而非客户端授权），按真实消费者枚举 delta/thinking/tool/tool_done/title/done/error/no_active。握手不是长期授权凭据，每个敏感事件仍实时检查。

授权/来源/协议错误必须清除本轮 partial、trace、文件卡与 portal 并停止重放；只有显式忙码表示“新命令尚未接受”，不能把任意 409 自动重发。EOF/未知帧/坏 JSON 不是 done。attach 需要完整握手和当前授权，不能仅因为 UI 显示 generating 就接受缓存帧。捕获 token/snapshot/session/run 的 owner 隔离优先于 activeId；迟到上传、预览 Blob、下载保存均经当前 capture commit。

最低验证向量：read-only 不 run，create-only 不继承 run，attach-only 可读取合法运行但不能另发 LLM，cancel-only 可停止本人任务且不返回正文；source 撤权同时阻断 history/attach/file；模块 use/run 撤销阻断下一网络/发布边界；A→B→A 迟到帧不恢复；未知旧 provenance 不读取；preview-only 不下载图片；session/file 同名不同 stable ID 不接管；真实 SQL 在拒绝时不物化 messages.content/tools；run 内容增长、checkpoint updated_at 不制造自身授权 revision 失效（revision 只绑定真实 owner/生命周期及本次固定命令）。以上是下一阶段执行约束，不是测试通过报告。

具体 SSE 字段以 [S4 SSE 唯一合同](s4-agent-sse-wire-contract.md) 为准；root 已批准独立 generate 工具动作、既有 sidecar 的 Agent 文件绑定与 authz_s4 持久协议守卫。strict epoch/catalog 历史失效保持，不因执行动作与来源分离而允许历史跨 epoch rebase。
