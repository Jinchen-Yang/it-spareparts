# Warehouse 旧 worker 与首次切换的事务边界

这是 S4 后台入口统一检查发现的 S3 遗留接缝修复，生产未操作。修改仅为 `api/maintenance_warehouse.py::_apply_worker` 创建 Session 后、任何业务解析/领域锁之前调用共享 `runtime.guard_legacy_execution(db, ('maintenance_warehouse_import',))`；既有 apply/commit/rollback/finally 保留。

原公开 worker 接收排队时捕获的旧参数，资源已切换或切换后撤配置仍写入真实仓库事实。两个真实 PG 反例先红（2 failed、正常旧路径 1 passed，`/tmp/itsp-s4-warehouse-worker-cutover-red.log`）。现在这两路返回 503 `AUTHZ_LEGACY_EXECUTION_RETIRED`，捕获 SQL 确认未查询仓库/来源/型号事实，Batch/Document/Audit 均零写。

共享 runtime 由另一作者实现并拥有：资源排序事务级共享 advisory、READ COMMITTED 当前 marker 检查；首次 activation 同键独占锁。worker 自己不提交守卫事务，锁持有至原业务 commit/rollback。新增实际两连接反例在仓库事实 flush 后暂停 worker，通过 `pg_blocking_pids` 证明独立 activation 被该 worker 的事务阻塞；提交之前另一连接看不到事实或 marker，释放后事实与 marker 先后完成，旧幂等重放也拒绝。未用固定 sleep 或 mock PDP allow 代替并发证据。

新文件 `test_authz_s4_warehouse_worker_cutover.py` 4 项，加 S3 canonical warehouse 和旧 HTTP API，原样 24 passed（34.92s，`/tmp/itsp-s4-warehouse-worker-cutover-green.log`）。正常旧 apply 与幂等回放、规范 Grant/sidecar 路径均保持。跨作者独审交 backend/root；本记录是修复作者证据，不自签独立批准。

外部复核：backend_surface_inventory 已独立检查入口 guard、原 commit/rollback 持锁范围，并原样运行新增四例：4 passed，证据 `/tmp/itsp-s4-warehouse-worker-cross-review.log`。该复核没有改动作者测试或生产文件。
