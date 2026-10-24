# S3 导入、来源分配、取消与 WBDD 独立复审

本次复核覆盖 16 个真实入口：document/CKD imports 7、source auto 1、source cancellation 4、WBDD 4。返件 import 6 的独立证据另见 `s3-return-import-independent-review.md`，合为作者最后 22 个入口。这里的通过仅代表当前本地独立核对，不代表完整 CI、用户验收或生产部署。

原四个作者测试文件先独立原样运行：`test_authz_s3_document_imports.py`、`test_authz_s3_source_assignments.py`、`test_authz_s3_source_cancellation.py`、`test_authz_s3_wbdd.py`，27 passed（`/tmp/itsp-s3-workflow-cross-review.log`）。随后新增 `backend/tests/test_authz_s3_workflow_independent.py`，五个真实 HTTP/PG/PDP 反例全部先红（`/tmp/itsp-s3-workflow-independent-red.log`）：

- doc 与 CKD preview，以及 WBDD upload 三个入口忽略 multipart 的 authorization_precondition。原文件授权后换成有效但实际 bytes 不同的 ZIP，旧实现仍提交；修复后 409，零新批次。query/form 冲突也统一拒绝。
- source cancellation 的授权视图预先读取完整来源订单，包含客户/联系人字段。修复后只读取实际来源 ID、批次、当前状态和完整新旧项目关系。
- WBDD reconcile 的授权视图预先读取完整订单/行，包含成本。修复后只读取真实 selection 的行/来源 ID、状态、项目 assignment/version。

逐项静态核对：source auto 在完整领域工作和新项目创建后，一次按稳定 ID 锁齐 actor 与实际 owner candidates，并重验 salesperson 映射；最终新项目使用真实 ID 再求当前 Grant，未用预检 input 作为持久来源。来源取消保留旧/当前关联拒绝、稳定 owner 回执与最终 actor/PDP，未将 username 作为 canonical 命令身份。WBDD reconcile 的 scope Plan 在候选 digest 之前；实际每个 target 及历史回执 parent 完整全称检查，无父来源仍是真实 source subject，只有实际 all-projects PDP 结果可通过。

追加核对 WBDD report 接缝：`_sync_context` 只读取 JSON 中 parent/source 关系与 snapshot opaque digest；`_sync_state` 在 SQL 内计算完整回执摘要与 owner，不将 report/结果财务载入应用。作者新增真实 SQL 回归及完整回执篡改 403 断言，由本复核原样重跑。生成 digest 并未替代当前对象授权。

imports 的同型读取缺口经 root 明确分派，由本复核者协助修复 `maintenance_import_authorization.py`，因此这部分代码修复不自签：SQL 内对不可变 raw columns 聚合 SHA256，应用仅拿摘要；header/report、head-to-project 解析均改为关系元数据。WBDD→XSDD→project_code 的原解析顺序未变，实际 apply 的业务解析仍在当前授权之后。新增 `backend/tests/test_authz_s3_import_projection.py` 四项检查读取/apply 授权视图实际 cursor projection；Astra 另作独立复核。

最终独立组合 **41 passed（72.09s）**，`/tmp/itsp-s3-workflow-cross-review-final.log`：上述四作者包 28（含新 WBDD report 例）、独立五红转绿、imports 投影 4、返件独立 4。另 imports 作者 10 + AI 接线 14 + 最初投影 2 为 26 passed（`/tmp/itsp-s3-import-opaque-digest-first.log`）。集合有重叠，不合并为唯一测试总数。

持久证据均在 `backend/tests/`，不能只凭临时日志或 registry 注册条数认定接线完成。当前上述原独立反例已闭合；imports 修复的第二作者独审回执另补，整体 S3 发布门由根代理统一裁定。

外部复核补充（authz_phase_review）：独立读取 imports opaque-digest 修订及实际元数据解析，未发现新增阻断；原样复跑 `test_authz_s3_import_projection.py` 四例与原作者十例，共14 passed（28.81s），`/tmp/itsp-s3-import-metadata-independent-final.log`。实际存储字段完整性仍包含原始与归一化数据，SQL只回摘要，解析优先级与完整当前/历史父集合保持。该修复完成跨作者闭合。
