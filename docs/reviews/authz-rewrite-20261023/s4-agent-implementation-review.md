# S4 Agent 执行与私有产物实施记录

本包在 S3 exact c0396fc08f6d5084358ebf352ffedad717980274 全量 CI 成功后实施。这里只记录开发验证；S4 完整 CI、统一用户验收和发布仍未完成，未操作生产。

## 实际执行链

- 13 个真实 HTTP 入口及 `TOOL agent.write_excel`、`TOOL agent.write_report` 使用唯一 registry 的精确 entry、default/context_0。主体为验签实名账号，私有 session/file 只允许实际稳定 owner；没有管理员读取旁路。
- `ExecutionContract` 复用核心 `_freeze`，递归不可变；原输入及嵌套 dict/list 都不能在后续重验中换成另一份命令。执行许可与内容来源分别处理：use/run/upload/generate 每个实际边界当前复验，但不作为所产内容的历史读取来源；领域数据读取和文件 preview 仍完整记录并继承。
- 复用 `AgentAccess`/`AccessCollector` 和 `service.access_recorder`。实际 worker 线程及每次 generator advance 内安装 ContextVar，异常/跨线程推进后恢复。每次模型调用、模型 chunk、工具、hub 发布/消费、checkpoint、历史和文件访问均重验。不得跨 LLM/慢端保留用户锁。
- 历史先读取消息 ID/role/provenance，全部来源通过后才物化 content/tools；cleanup 的 cancel/delete 独立授权，不强迫读取失效历史。稳定 session revision 不包含本轮新增消息和 updated_at。
- 私有文件复用 `SysAuthorizationArtifact`，adapter 固定 `authz_s4`，默认 S3 adapter 行为保持。安全 header 只有 owner/generation/ext/kind 及 SHA；当前 PDP 通过后才加载完整 metadata 与 bytes。header SHA、metadata SHA、实际 bytes SHA、DB binding、稳定 owner/generation 和来源相互对拍。派生文件每次读取还重验实际基文件 bytes，不能只看旧 Grant。
- 新文件 header/meta/bytes 独占创建；失败只补偿本次创建的路径。已有相同 file_id 不会被覆盖或误删。绑定注册与实际主体复验同事务；独立协议哨兵在领域/用户锁前持久化。提交后撤权不会把已登记文件当临时文件删除。
- SSE 使用唯一冻结协议：ready 首帧、真实共享 context、同 run 连续 seq、闭集事件/错误、明确 terminal。完整 attach 从 1 回放；buffer 缺段返回 ready false + CHAT_REPLAY_GAP，零内容。无活动 run 为 null + no_active；响应预检后的撤权仍输出 ready + typed error。checkpoint 失败不能伪装 done，未知 EOF 为协议错误。
- 会话列表、历史及文件 preview 正式 JSON 均带实际 `authorization_context`。list 和 preview 的 wire 来自数据读取同一 RR 观察；工具嵌套 preview 不覆盖父 run 的 ready context。图片 preview 只返回元数据，不能自动借 download 读取像素。

## 入口与持久验证

| 入口组 | 实际门 | 主要测试 |
|---|---|---|
| POST chat / chat/stream | agent_chat.use，实际 user-only 命令 | test_authz_s4_agent、agent_lifecycle、旧 S2 lifecycle |
| GET/POST sessions；PATCH/DELETE session | private_chat_session.read/create/rename/delete，真实 owner | test_all_session_crud_and_no_active_wire、foreign owner、旧 stable owner |
| GET session/messages | private_chat_session.read + 每条实际来源 | unknown_lineage SQL、旧 HTTP epoch/history |
| POST session chat/stream / attach / cancel | run+use / attach / cancel，精确 session | actual_worker、checkpoint_failure、epoch_chunk、capped_attach、attach-only |
| POST upload；GET file/preview；GET file | upload / preview / download + actual owner/header/bytes/source | test_authz_s4_agent_files、multipart CAS、data_response_context |
| TOOL write_excel / write_report | 父 use/run + generate，基文件另需 preview | 真实派生报告、同 epoch 权利拆分、无隐式 preview/download |
| maintenance 三个只读工具 | 对应 maintenance_cost_access HTTP facade | test_authz_s4_agent_maintenance_tools 三项真实结果/SQL/来源对拍 |

本机真实 PostgreSQL + Cerbos，外部模型使用合成 provider，未 mock PDP allow：

- 新旧 Agent 组合：124 passed、1 skipped，`/tmp/itsp-s4-agent-combined-first.log`。这是正式 JSON context 修订前的完整组合，不能冒充修订后完整回归。
- 正式 JSON context 修订后，真实主 HTTP 10 passed：`/tmp/itsp-s4-agent-data-wire.log`。
- 维保工具三个当前 HTTP 结果/SQL 投影/精确来源对拍通过：`/tmp/itsp-s4-agent-maintenance-tools.log`。
- shared adapter protocol 先红后绿，S4 注册不能借 S3 marker；含 S3 artifact 回归 16 passed，`/tmp/itsp-s4-artifact-protocol-green.log`。
- 根独审提出深不可变缺口，先复现嵌套修改未拒绝，再复用 `_freeze`；原输入修改不改变实际 ref/revision，内部两层修改均 TypeError。
- 根独审指出 list/preview 正式响应缺 wire；修后真实响应输出 `s4-agent-http-wire-golden.json`。前端 `s4AgentWire.test.ts` 使用真实 validateCatalog/validateAuthzSnapshot/validateContextualPayload/consumeSSE 原样消费，2 项通过，未伪造 catalog/epoch/context。
- 生成工具仅返回 safe file_id/ext 后，系统提示与 write_excel/write_report 说明同步改为真实 ID 的定位链接，不再要求 canonical 不提供的 download_url。真实 worker 生成文件、第二轮模型消费安全输出、SSE 与持久历史链接、独立 preview/download 完整通过；lifecycle 全文件 7 passed，`/tmp/itsp-s4-agent-report-contract.log`。实际合成结果保存在 `s4-agent-generated-file-wire.json`，另由前端独立测试消费文件卡片；生成不自动附送读取权限。

## 旧测试适配边界

旧 S2 lineage 测试增加显式 S4 三资源 Grant、资源启动及当前命令绑定，仍保留撤 epoch、Grant version、未知来源、跨主体、发布前撤权、队列撤权、LLM/tool/checkpoint 零写断言。文件 preview 是实际新来源，因此精确增加 schema2 file dependency 和全局依赖的四个 null 字段；没有丢弃旧 inventory 来源。

S4 已登记执行合同之后，撤配置变为 suspended，缺 cutover 不能执行，不能继续沿用 S2 尚未迁移 Agent 时“有空来源即可上传/读取”的预期。错误按现闭集 AUTHN_SESSION_INVALID 区分失效登录；SSE 增加 ready 首帧。旧 closure 名称检查改为全部 13 个实际 mounted HTTP 的旧权限拒绝测试；旧工具异常脱敏测试增加标准 db 清理，避免前序持久 cutover 污染，业务断言保持。

## 跨模型审查状态

本包由 root 正在独审；本记录不是作者自签的最终批准。根的平台/公共上下文/机器包由本代理独立读取并复跑：平台与核心 20、机器 30、额外 partial-cutover/资料注入 2 通过。exact-entry dispatcher 两个真实批次毒针先红，root 加入 entry/explicit branch/observation 核对；独立文件再增加 reference 错标反例。原样复跑该文件、平台独立测试及 Agent HTTP/lifecycle/files/artifact-protocol，37 passed（56.49s，`/tmp/itsp-s4-agent-dispatch-final.log`），包括正式 JSON context 修订。所有共享核心修订由 root 单 owner 完成。

reference 重验使用持久依赖自己的 entry/branch，不使用外层消费者入口替换历史合同；未显式指定 branch 的真实发现仍可选登记分支，显式指定则必须严格匹配。错误注册回调是受控内部毒针，不表述为外部客户端可任意注册 resolver。
