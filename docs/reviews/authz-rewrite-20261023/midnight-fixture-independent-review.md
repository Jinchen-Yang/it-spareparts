# S0 午夜日期夹具修复独立复核

范围：基线 `edd0f3efade00ab457731737f82f68eb282a2226` 上的 `backend/tests/test_maintenance_migration_source.py` 工作树差异。只读检查实现及既有运行日志，不修改业务代码，不代表全量 CI 已完成。

结论：本小修无阻断，可提交并运行精确提交 CI。

- 原默认参数 `warehouse_ready_through=business_today()` 在测试模块导入时求值，后续 `_build` 的 `as_of` 在执行时求值；测试收集与执行跨上海午夜会得到不同日期。这与 `/tmp/itsp-authz-midnight-probe.log` 中注入旧默认日期后出现的 `source_snapshot_hash` 不相等一致。
- 新 sentinel 只在参数省略时使用同次调用的 `frozen_as_of`，不重新读取时钟。显式 `None` 仍表示缺少完整性水位；`test_zero_warehouse_rows_need_a_signed_completeness_watermark` 的空值及阻断断言保留。
- `test_snapshot_binds_one_shanghai_business_date` 改走真实默认路径，保留时钟只调用一次的断言，并增加水位与冻结日期相等的断言。原审批状态用例的源 hash 相等、审批 fingerprint 不等两个核心断言未改动。
- 审批状态用例自身也仅在开头读取一次 `snapshot_date`，pending、重建及 approved 三次构造显式共用其 `as_of` 与水位。这样即使用例自身跨午夜，也只比较同一业务日的审批状态差异；原 hash/fingerprint 断言完整保留。
- 已读最终 `/tmp/itsp-authz-midnight-final.log`：该文件 **18 passed，4.43 秒**；独立运行 `git diff --check -- backend/tests/test_maintenance_migration_source.py` 通过。没有为本次文档复核重复创建测试数据库。

验证边界：本修复闭合该文件默认参数及审批状态用例的跨午夜依赖，不宣称全仓所有测试都完全摆脱时钟。业务日期逻辑未变，合并与阶段推进仍以新 SHA 全量 CI 为准。
