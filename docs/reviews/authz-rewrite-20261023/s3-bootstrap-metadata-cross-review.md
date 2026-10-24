# S3 bootstrap metadata 修订交叉复核

本报告记录非作者对最终 metadata 收口的源码复核和原样真实 PostgreSQL/Cerbos 回归。它不替代精确提交的完整 CI。

## 仓库与补库

独立复跑 `backend/tests/test_authz_s3_bootstrap_metadata.py`、`test_authz_s3_warehouse.py`、`test_authz_s3_replenishment.py`：**31 passed**，日志 `/tmp/itsp-s3-warehouse-replen-metadata-independent.log`。四个原样 SQL 反例分别覆盖：无 apply 的仓库历史回执不加载完整 `result_json/after_json`、无 resolve 不加载候选与证据 JSON、无补库 read 不加载申请业务快照、无仓库 preview 不加载型号描述与分类。相关原用例继续覆盖真实授权成功、完整父集合/上游递归、撤权锁等待、回执篡改、sidecar、scope 分页和只读快照。

源码核对：

- `maintenance_warehouse_authorization._receipt_metadata` 读取真实 batch 主体、输入摘要和不可变授权计划；业务结果只在 SQL 中散列。完整回执与结果 hash 对拍仍由获准后的 `_receipt` 执行，未删除篡改检查。
- `_resolve_plan` 的候选匹配在 SQL `EXISTS` 中完成，候选和证据仅返回不透明摘要；`_input_plan` 只查实际 PN 对应的 `DimPart.id`、有效 alias 与返件 ID/父关系，仍保留原件完整目标集合。
- 补库 `_resolve` 明确使用 `load_only(..., raiseload=True)` 的身份/版本投影并 `populate_existing=True`；正式读取只有在 `require_complete` 和 precondition 验证通过后才取完整申请/草稿并检查版本 sidecar。
- 两包原有初始 RR 授权和最终完整域锁后 actor/PDP 重验均保留；本轮未改变业务解析或事务提交粒度。

未发现该修订的新阻断。此结论只覆盖上列包与用例；其它领域 metadata 修订及最终 mounted inventory 另行收口。

## 清单、月报、项目工作簿与 master 回执

独立复跑 `test_authz_s3_workbook_bootstrap.py` 的四个真实 SQL 拒绝反例，并与 checklist、monthly_workbooks、project_workbooks、master_workbooks、master_independent 及本次独立篡改反例组合：**48 passed**，日志 `/tmp/itsp-s3-workbook-metadata-independent-final.log`。

月报只通过 SQL 提取真实项目 ID 集合并返回完整不可变 plan/issues 的摘要；项目工作簿验证批次只返回身份/状态与完整 plan/issues/error bytes 的 SQL 摘要。两者 producer 与 state loader 使用同一方法，原件完整性没有因为取消 Python 全量读取而被删除。原 scope 集合等式、owner、当前 Grant、precondition 和实际域锁后 actor 重验仍保留。

Master replay 的预授权读取只含 Operation 的身份/幂等字段、Audit 的封闭授权合同及完整 Audit 的 SQL 摘要。授权后仍执行原 schema、operation key、response hash 与已发布 manifest 对拍；没有把存在合法关系元数据等同于回执可以直接返回。

本轮独立发现并修复了另一个真实缺口：清单 sidecar 原来只绑定上传文件 hash；修改已落库 Item 的 `requirement`、`done` 或 `raw_json`，保留文件 hash，正式 apply 都仍返回 200。`test_authz_s3_workbook_metadata_independent.py` 的三个参数化反例原样记录 **3 failed**（`/tmp/itsp-s3-checklist-binding-independent-red.log`）。作者现将完整 immutable batch 身份/输入字段与所有 Item 列按 row_no/item_id 稳定排序在 SQL 内聚合散列，producer、state、context revision 同用此摘要；排除 apply 合法修改的 status/applied/replaced/report。三个原反例现均 403，批次保持 pending，包含在上面独立 48 项中。原有允许、改名、重放与旧业务测试没有删除。

上述工作簿修订独立复核通过，无剩余已知阻断。最终总入口清单仍等待 root 其它 metadata 修订及 main 冻结确认后统一重跑。
