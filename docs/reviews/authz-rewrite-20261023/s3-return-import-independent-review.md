# S3 返件导入六入口独立复审

复核真实 submit、job read、retry、cancel、raw、apply 六入口，覆盖 `maintenance_return_import_authorization.py`、实际 worker/业务原语、API multipart 和发布路径。独立新增 `backend/tests/test_authz_s3_return_import_independent.py` 四项真实 PG/PDP/HTTP 反例均先红后绿：

1. RAW 已在 RR 中物化完成、API 尚未发 bytes 时，第二连接撤 epoch；旧实现仍 200 发布，修复后拒绝。
2. 同一发布窗口删除实际 artifact binding；旧实现仍发布，修复后重新核真实 binding 并拒绝。
3. 旧文件授权后在 multipart 正式提交替换实际文件，同时携带旧 precondition；旧实现提交，修复后 409 且零批次。
4. 无 RAW action 的账号访问授权视图；旧实现加载完整 project note/receipt evidence。现在先验 typed 当前主体与真实 stable owner，只取关系 ID/parent/version、所需 JSON 关联键与摘要；完整业务计划在当前授权后读取。

第四项 SQL 断言检查 SELECT projection，而不是整个 SQL 的任意词：WHERE 中从 JSON 提取实际 head ID 进行关联是合法关系解析，不等同于把 source_payload 完整载入应用。

静态核对还包括：完整原件的排除类别不能在权限检查前丢弃；unknown 工作表不能当作空依赖；retry-only worker 的 producer 动作确为 retry，不能借 submit；排队后/临发布再次撤权不写 raw/plan；历史 NULL owner 不可接管；同稳定 ID 改名与同名新 ID 分开；实际 sidecar、input digest 与生成代次在当前读取重新核验。RAW 采用当前 reader 的 provenance 和公共 bounded spool，首 headers 与每个 64 KiB chunk 都重新检查，未拿 producer 身份代替 reader。

独立红证据：`/tmp/itsp-s3-return-import-independent-red.log`（2）、`/tmp/itsp-s3-return-import-multipart-red.log`（1）、`/tmp/itsp-s3-return-import-sql-red.log`（1）。修复后作者 9 + 独立 4 + global 10 合计 **23 passed**（`/tmp/itsp-s3-return-global-independent-final.log`）；原独立 4 在最终 workflows 41 项组合中再次通过（`/tmp/itsp-s3-workflow-cross-review-final.log`）。

当前本包无剩余独立阻断；这不是完整 S3 CI、用户验收或生产上线证明。
