# S4 平台、公共上下文与前端独立复核

本代理著作 Agent 后端；本记录只独立评价其他作者的平台/公共上下文/机器与前端实现，不将自己的 Agent 作者测试当跨模型批准。未进行生产操作或最终浏览器验收。

## 平台与公共上下文

已读 `control_access`、`principal/service/management`、accounts/authorization、boundary/machine、唯一 `domain_contexts` 及 maintenance re-export。普通账号目录与资料更新分别使用当前 Grant；实名管理员控制恢复入口不附活动/旧权限 blob。普通 admin 无业务读旁路，profile 更新的实际用户锁按稳定 ID 一次收齐，最终当前权限和目标管理员状态重验。机器入口先核封闭 credential/action 再业务 SQL。静态未发现其余阻断。

- 作者平台与公共上下文测试原样独立复跑 20 passed；机器 30 加本代理原两例共 32 passed。日志 `/tmp/itsp-s4-control-context-independent.log`、`/tmp/itsp-s4-platform-machine-independent.log`。
- 本代理两例验证 directory-only cutover 不使未切换 profile Grant 越过旧拒绝；profile Grant 注入 role/password/is_active/source 整体拒绝，安全变更仍实名管理员门，零资料 audit。曾因共享文件同名覆盖丢失，现从原始工具记录逐字恢复到唯一 `test_authz_s4_platform_astra_independent.py`，原样 2 passed，`/tmp/itsp-s4-platform-astra-restored.log`。另一作者的 8 例仍独立保留在 `test_authz_s4_platform_independent.py`，未混称自己的反例。
- 发现合法批次被内部错误 resolver 返给另一 entry/explicit branch 的缺口；两个真实 PG 批次毒针先红，root 加 `_checked_dispatch`。再加历史 reference 错标实际 entry 的反例。三例连同平台独立与 Agent 关键回归原样 37 passed，`/tmp/itsp-s4-agent-dispatch-final.log`。

reference 必须按持久依赖自己的 entry/branch 重解析，不能借外层消费者改写历史合同。未显式指定 branch 的发现阶段仍可选择登记分支，明确 branch 则必须一致。毒针模拟受控内部 callback 编程错误，并非声称客户端可注册 resolver。

## 前端独立复核

范围：rules.ts display dependency、contextQuery/domainTransport 的 resolvedRule、CanonicalImportPage、useChatStream/ChatPage、文件卡片和下载发布。未重新审全体 S2/S3 UI。

确认的目标生命周期阻断：会话 A 上传在途，新建 B 或删除当前 A 后旧 upload 捕获仍有效，迟到响应重新显示旧附件。独立真实 AuthzProvider 测试 `S4ChatLifecycle.independent.test.tsx` 两例先红，日志 `/tmp/itsp-s4-chat-target-independent-red.log`。作者统一 `switchTo` 增 generation/清投影/abort，并让 upload catch/finally 同时匹配目标代次；原零旧附件、零旧成功提示、AbortSignal 已取消、不重传断言均保留并转绿。测试固定桌面 breakpoint，按钮允许真实图标带来的 accessible-name 前缀，不放宽授权断言。

另使用真实 PG/PDP/write_report/worker 返回的实际 file_id 与正文定位链接（`s4-agent-generated-file-wire.json`）验证 UI 渲染文件卡片；只有 preview 展示/动作时只发其实际授权视图与 preview 请求，不附 download。此 UI 测试的观察 fixture 明确为合成，实际 HTTP/context 闭合另由 `s4AgentWire.test.ts` 原样真实 golden 验证，未混用两类证据。

其余已读接缝：

- display 依赖只判断同模块声明完整性，实际 surface data_rule 和当前 exact entry 仍独立解析；data/mutation 不因 show 的 requires_context 被视作允许。
- 导入实际来源由服务器 context 选择；resolvedRule 在 formal 前校验该来源的 show 和实际动作，POST 全称 AND，GET 仅已许可块 OR，未从文件名猜来源。报告/状态同资源配对，局部作业隐藏全件数量/状态；确认后的文件或模式变化清旧准备结果并换请求代次。
- SSE 当前 snapshot/目标/输入捕获贯穿预检、正式 fetch、ready/context 校验与逐帧发布；闭集 seq/terminal/EOF 错误不自动重发。blob 创建下载链接前再检查当前捕获。FileCard preview 不借 download。

StrictMode import cleanup 是 root 已发现、作者另修的独立问题，不冒充本代理发现。冻结后原样复跑 Chat 三独立、Chat 六作者、Import 九作者、SSE 三十三、真实 Agent HTTP golden 两项，共 5 文件 53 passed（`/tmp/itsp-s4-frontend-cross-review-frozen.log`）。双 locator 作者反例也已闭合。纯 Axios `domainApi` 抽取未改变有 capture 必走受控 transport 的行为；未将循环导入修复当权限放宽。

在上述审查范围内没有剩余阻断，支持进入精确候选全量 CI；此结论不替代整个前端全量、真实浏览器或用户统一验收。

## Legacy Agent 短事务与文件发布最终复核

新增独占 `test_authz_s4_legacy_file_publication_independent.py`，原实现三个真实反例均失败（`/tmp/itsp-s4-legacy-file-publication-red.log`）：相同 file_id 覆盖原件、metadata 写失败留下新 data、非 Agent 的首次 `sales_analysis` marker 越过暂停中的文件发布。

backend 作者修复后，独立原样运行这三例、其 checkpoint 三例、canonical Agent files/lifecycle 与旧 BOM：**28 passed**，`/tmp/itsp-s4-agent-global-barrier-independent.log`。碰撞仍必须抛 `FileExistsError`；失败前已存在的文件逐字节保持；新候选补偿零残留；跨连接以 `pg_blocking_pids` 证明非 Agent 首次 marker 等待真实发布事务，结束后恰有原 bytes 和 metadata 两文件。

静态复核范围是 runtime global transition guard、chat_store 写入口和 agent_files 三个 producer。global exclusive 在 activation 的 sorted resource exclusive 之前；legacy 短写 shared 在实际 row/actor 锁之前，未包围 LLM；格式渲染在文件短事务之外，data 与临时 meta 使用独占创建，最终 hardlink 不覆盖已有 meta，异常仅清本次成功创建路径。canonical mark_used 仍在 actor/领域锁之前。未发现此范围内新增阻断。进程配置变化不是数据库事务事实，本结论不把配置约定称持久保证。
