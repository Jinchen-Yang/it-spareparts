# S3 批量转项目独立复审

当前结论：本报告的三个独立真实 PostgreSQL/Cerbos 反例均已通过，包括首轮发现的合同移父 OCC 阻断。另一个复审者补充的所选行 SQL 投影与锁序反例由作者修复，仍等待其独立确认；不能据本报告提前宣布批量转项目整体冻结。复审者没有修改作者生产代码。前端、完整 S3 CI 和生产验收是不同证据。

复核范围：`maintenance_batch_transfer_authorization.py`、`maintenance_bulk_import.py` 的 canonical 原语接缝、批量转项目 API，以及实际批次、逐行选择与 artifact binding。

已经独立通过的反例：

- 真实工作簿创建 A、B 两个项目，再上传两个项目的金额更新。将当前授权收窄为 assigned_projects 且仅分配 A：选择 A+B 拒绝且批次保持 processing；只选择 A 成功，A 金额从 113 变成 226、B 保持 113。A 的持久回执可重放；撤掉当前 apply 后回执也拒绝。未选 B 不得进入所选操作的授权合同。
- 真实 apply 已更新两项目并成功登记持久 receipt binding 后注入异常：两个项目金额回滚、批次恢复 processing、artifact 数量恢复，之前成功预览的两个原件路径与 bytes 保留。

首轮真实阻断（现已修复）：收款裁定授权视图返回 precondition 后，capture 完成，独立 PostgreSQL 连接将真实合同从项目 A 移到 B 并提高版本。使用原 precondition 正式裁定仍返回 200，旧 receipt 被替换。预期为 409 且裁定零写。当前 `_finish` 将 precondition 与已捕获上下文自身比较；它重新授权当前 parent，但没有证明原关系 revision 未变，RULING 原 material 也没有固定旧 project_id。

测试文件：`backend/tests/test_authz_s3_batch_transfer_independent.py`。首轮 **2 passed / 1 failed**（13.81s），日志：`/tmp/itsp-s3-batch-transfer-independent-first.log`。红例：`test_ruling_contract_reparent_after_capture_invalidates_precondition`。作者已固定 original_project_id，最终锁定原/当前关系后重新取得 current whole-contract PDP，并用冻结 material 重建当前上下文比较原 precondition；不再把 captured precondition 与自身比较。三条原样测试复跑 **3 passed**（13.23s），日志 `/tmp/itsp-s3-batch-transfer-independent-green.log`。随后作者与本报告组合 **14 passed**（23.45s），日志 `/tmp/itsp-s3-batch-transfer-independent-final.log`；根代理另报告作者、本报告三条和 tail 四条组合 19 passed，属于其组合验证证据。

另两处静态接缝已由作者修复：options 正式响应附 authorization_context，并以真实测试比较 view/data；finish_apply 不再把全部 original_plans 带回所选目标合同。上面的真实 A/B 测试验证了后者。PREVIEW 授权前计划读取业务 ORM 的问题由作者另行修复，本复审未重复修改或将其计为已验证。

最新作者 metadata-only 与所选计划 SQL 投影修订后，本报告三个独立反例再次原样通过，包含在主应用/旧 Beta/独立批量组合 **52 passed**（65.70s，`/tmp/itsp-s3-main-protocol-final4.log`）。静态复核所选计划通过 JSONB 行键投影，事务末项目→合同→actor 锁序、当前完整目标 PDP、原关系 fingerprint 比较与自己业务版本变化分开；本复审者没有修改这些生产实现。Astra 的两个追加反例由其单独确认，证据不重复计数。
