# S4 前端剩余入口差距（只读，未实施）

首轮盘点基线 `68e9944`；下文目录数量保留当时快照，不代表 S3 实施后的当前计数。S3 收口期间新增的 Chat/SSE/Import 协议对拍见 [S4 前端协议接缝](s4-frontend-protocol-proposal.md)。S4 仍只读准备，需独立阶段门后开工；不把旧 189 surface 草案当实施证据。

目前 S4 新接入的真实业务页面只有 **`/import` 和 `/chat`**。聊天历史、文件预览/下载、生成中任务均嵌在 ChatPage；导入历史、作业轮询和批次报告均嵌在 ImportPage。没有单独的文件中心、聊天历史或系统任务路由。`/system-settings` 是 S2 的 display_policy 页面，不能因为名称含 system 再登记成任务后台。

## 目录状态与真实实现要分开

首轮目录快照为 87 项，其中 23 implemented、62 planned、2 retired。排除随后由 S3 新闭集替代的 47 个维保/补库候选后，剩余 15 个 planned 元数据如下。这个历史数量不限制 S4 最终拆分。

| 元数据 | 当前前端位置 / 已有保护 | S4 要收口的事 |
| --- | --- | --- |
| data_import | `/import`，原 page_import 路由；尚未用通用 access/capture | 接生成 surface、分别控制预检/提交/历史/轮询/报告/错误文件；域、字段和 job/batch 范围须后端先冻结 |
| agent_chat、private_chat_session、agent_file | 同一个 `/chat`，原 page_chat 路由；后端已有 S2 实时身份与来源血缘保护 | 接新目录动作、生命周期、对象/文件能力；继续复用 S2 来源检查，不能为新 UI 再造后端真值 |
| account_directory、account_lifecycle、legacy_template、authorization_admin | `/accounts` 已按 S1 actor capabilities、原子编排及版本协议运行；这四项目录仍 planned | 明确它们是平台管理边界的描述还是要变为业务 Grant，消除元数据歧义；不得重新打开旧 boolean 表单或将现有控制面说成未实现 |
| resale_return_feed、resale_catalog、resale_price_reference | 无本仓库 React 消费者；机器接口合同 | 后端/服务主体包验证，不能给员工添加虚构菜单或复用管理员页面授权 |
| authentication、authorization_snapshot | LoginPage、App、ChangePasswordModal、AuthzProvider | 保留身份启动与本人会话能力，不用尚未取得的 `/me` Grant 授权 `/me` 自己；继续验证登出/换号/改密边界 |
| health_probe、framework_documentation | 没有 React 页面调用 | 后端公共/运维边界明确登记；不造用户权限项 |

更新日志是 `AppShell` 内静态 CHANGELOG Modal，没有日志接口或任务数据。`renameChatSession` 虽有 API helper，目前无 UI 调用；两个无状态 `/agent/chat` 端点、`GET /import/jobs` 和单文件 `/import/upload` 同样不是当前页面的独立入口，仍须后端闭合，不能因此恢复旧页面。

## 真实页面、请求与缺口

下面 action 名来自现有 planned 目录，仅供接缝建议；最终由后端 executable 定义冻结后生成。读取不自动授予 show，多个独立 surface 不能被一个总 read 门吞掉。

| 真实面 / 挂载点 | 实际请求 | 必需收口 |
| --- | --- | --- |
| ImportPage 文件暂存、ImportPrecheckPanel | POST `/import/precheck` | data_import.preview + show_upload；暂存本地文件不等于已获提交能力。现有 mode/文件顺序/input revision 校验继续保留 |
| 修复模式 ExpenseVoidPreviewPanel | POST `/import/expense-void-preview` | 不是普通文件格式预检：返回待作废报销事实与金额，须按完整业务范围/字段合同保护；不能仅凭通用 upload/read 推导 |
| ImportPage 正式提交 | POST `/import/upload-batch` | submit + show_submit；预检 token、mode、文件指纹与当前 capture 一致；仅人工确认发送一次。提交未知结果不能自动重放 |
| 初始历史表 `loadBatches` | GET `/import/batches` | batch_read；当前页面一挂载就请求，须移到独立 query 门。现 show_jobs 命名与实际“批次历史”不精确，冻结目录时需明确归属 |
| 当前作业进度、继续查询 | GET `/import/jobs/{job_id}` | job_read + 真实 job 能力；当前 1.5s 轮询仅有组件 generation。授权/主体变化须 abort、清数据；401/403/503 不循环重试 15 分钟 |
| 批次详情 Modal、预检重复批次链接 | GET `/import/batches/{id}` | batch_read + show_report + 实际 batch 范围；旧输入变化/关闭已会 abort，但还需 actor/snapshot/object 绑定。报告计数/错误文本不能默认无敏感信息 |
| 批次问题 CSV | GET `/import/batches/{id}/errors.csv` | error_export + 完整文件内容合同。现 helper 内直接 Blob→anchor.click，须拆读取与当前 capture 的 commit，撤权迟到不保存 |
| ChatPage 桌面历史栏 / 移动 Drawer | GET `/agent/sessions` | private_chat_session.read + show_sessions；当前 mount 请求无 actor lifecycle，需独立 list query；会话标题与 generating 标记同样属于返回内容 |
| 历史会话 `openSession` / stream reload | GET `/agent/sessions/{id}/messages` | 真实 session owner + 当前来源合同。openSeq 只挡晚响应，切 B 时仍须首帧清 A turns、trace、文件卡和 portal；不能靠 Spin 遮旧内容 |
| 首次发言懒建会话 | POST `/agent/sessions` | create + 新会话命令能力，成功后再按真实 ID 检查 run。不能以创建成功附送后续所有会话动作 |
| 发言及“打断后续问” | POST `/agent/sessions/{id}/chat/stream` | agent_chat.use / private_chat_session.run 的最终组合由后端冻结；每个 SSE 事件与 finish 必须属于 captured owner/snapshot/session。当前泛 409 最多四次自动重发必须收口，不得把授权版本冲突当忙锁继续 replay |
| 切回生成中的会话 | POST `/agent/sessions/{id}/chat/attach` | attach 独立于新 run；回放也是当前来源读取，不能只凭 generatingIds 开始。拒绝/503 清旧 stream buffer/trace，不把半截敏感结果当成功历史 |
| 停止生成 | POST `/agent/sessions/{id}/chat/cancel` | cancel 与具体 session；当前 helper 重新读 localStorage token 且吞错误，需捕获身份。卸载断本地流与取消服务端任务要分开，不替新主体发送旧任务命令 |
| 删除会话 Popconfirm | DELETE `/agent/sessions/{id}` | delete；当前 catch 后仍本地移除，不应把权限拒绝伪装成成功。保留可解释错误，不重放 |
| 回形针上传 / pendingFile | POST `/agent/upload` | agent_file.upload；上传结果绑定身份与会话草稿生命周期，不让迟到文件附到另一个主体或会话 |
| Markdown FileCard / Modal 表格预览 | GET `/agent/files/{file_id}/preview` | preview + 整件来源再授权；不能把前 100 行预览当完整内容许可。缓存 pv/tab/文件名跟随 file+actor+snapshot，撤权关闭 portal |
| 图片 blob 与下载 | GET `/agent/files/{file_id}` | download；图片预览实际也使用下载端点，不能只有 preview 就暗送原件。Blob URL 创建/anchor.click 只在当前 capture commit，撤权/关闭回收 URL |
| ToolTrace、LiveTrace、复制当前回答 | 历史/流响应的附带内容，无单独端点 | tool args、返回片段、文件链接与正文属于同一获准响应，不能因只是 trace 就绕来源判断；清空后不能继续复制旧内容 |

现有 `api.ts` 的 Agent fetch helper 会再次读取 localStorage token；Import API 大多依赖 axios interceptor 注入身份。S4 需统一显式传入 captured token/signal，不能在 await 后借用新主体。`_consumeSSE` 目前只 JSON.parse+类型断言并跳过坏帧，应采用闭合事件 validator；未知授权事件/协议故障停止显示与流，不能当普通空事件跳过。需要区分服务端“运行忙但未接受”和版本/权限冲突时，应先冻结机器错误 code；在那之前不保留泛 409 自动重发。

## 共享文件与后台任务边界

- 维保 WorkbookRoundTrip、ReturnReceiptImport、验收附件、collection evidence、经理月报和 S3 import job 的文件/任务仍随 S3 所属资源迁移，不推迟到 S4；S4 做跨域生命周期复核。
- 通用导入可承载 maintenance/expense/workbook 文件。通用入口许可不能替代已迁 S3 对象写；预检、实际文件内容、worker 每文件事务及结果发布必须交对应领域合同。前端只编排服务器返回的完整预检/能力，不自行从文件名识别项目范围。
- 后台导入作业与 Chat worker 没有独立任务管理页；它们的 UI 是上述 progress/generating/attach/cancel。没有找到前端 cron、scheduler、system-task 管理调用。纯后台服务与商城任务应留在后端任务注册表，不据前端无页面判已覆盖。
- S2 已有 Agent 稳定 owner、历史消息/文件来源和逐块重验，见 `s2-agent-lineage-implementation.md`。S4 尚缺前端模块门与会话版本清理，不应重写现有 provenance 协议或声称 Agent 从零未保护。

## 最小实施分组与前置未决

1. **Import 包**：`pages/ImportPage.tsx`、`pages/import/*`、`api/imports.ts`。复用 `useAuthorizedQuery`/runner 与 S3 context adapter，保留现有内容预检、作废预演和业务状态机；不复制 Provider。
2. **Chat/文件包**：`pages/ChatPage.tsx`、`api.ts` 的 Agent 部分及测试。可拆 session controller、FileCard、SSE parser，但共用同一 capture/current/abort，不维护第二份授权状态。工具输出授权由服务端决定。
3. **共享集成**：生成 manifest/action/surface、nav/App、权限中心只读 planned 元数据分类；由同一前端 owner 处理。账号控制面与系统设置只核对协议，无重写需求。

必须先冻结：通用导入的 source-domain/字段及 job/batch 范围（旧页面全域可读不能擅改 owner-only）；聊天 use/run/attach 的确切组合及来源拒绝 DTO；文件 preview/download 原件权限；平台四个管理目录的定位；剩余 API-only 与服务主体处置。通用目录的 show/data/actions 都来自后端生成，新账号没有隐式 show。

有意义的反例：import upload-only 不读历史、history-only 不预检/提交；预检后撤权不提交；job/batch A→B→A 不露旧结果；错误 CSV 迟到不保存；chat history-only 不 run；新建成功但 run 拒绝；SSE token/epoch/session 改变后旧帧不落屏；source 撤权后历史/attach/文件同拒；preview-only 不取图片原件；删除拒绝不报成功；pending upload、Modal 与 Blob 全部随身份清理。全部尚未实施/运行，不将此清单当测试通过记录。
