---
name: partflow-import-review
description: 在 PARTFLOW 中检查已上传的业务单据、展示导入差异并引导确认，跟踪逐文件处理结果。
---

# partflow-import-review

先调用 `pf_get_capabilities`，确认 family、预览能力、发布状态及协议可用。工具未接通或 family 不支持时说明限制；不能改调直接写入接口冒充预览。

1. 缺少 file_id 时先通过上传流程取得；用 `pf_inspect_document` 识别，按 family/协议分组。优先默认 skip；只有用户明确提出修正既有数据时才考虑 upsert。项目身份使用稳定 ID/XSDD，不猜名称。
2. 调用 `pf_preview_import`。保存幂等键和返回的 job_id/preview_id。长任务按 `pf_get_job` 给出的轮询间隔查询，不能因网络超时重新上传整批。
3. 用 `pf_get_import_preview` 查看摘要及问题；展示实际选表范围、新建/覆盖/作废/跳过数、每文件状态、歧义、错误和 transaction_scope。说明多文件逐文件提交可能部分成功；分页时提示剩余数量，不能把首屏当全量。
4. 可提交时调用 `pf_open_review` 给出登录审阅链接。首版由员工在网页上审阅具体差异并确认，后台生成真实 approval。用户先前“导入这些文件”授权可用于完成准备工作，不等于绑定此刻差异的服务器确认记录；不要无意义地反复问准备工作权限。
5. 用 `pf_get_job` 核实原生回执，再报告成功、失败、跳过和待核对。预览成功、排队、正在处理都不称正式入账。

出现 stale_preview：重新预览并呈现改变后的差异。permission_denied：停止相关操作，不换账号。recompute_busy/rate_limited：按服务端间隔有限重试查询。result_unknown：先查原 operation/receipt，无法确定则保留未知并交人工处理，不假定失败或成功。

第二阶段仅当服务端明确开放 `pf_apply_import`，且存在有效网页 approval_id 时可提交；不可自己制造批准、勾选全部行、强制接管或自动作废。`pf_retry_job` 也只能用于服务端明确标记未提交且可重试的项，不能重放已成功或未知项。
