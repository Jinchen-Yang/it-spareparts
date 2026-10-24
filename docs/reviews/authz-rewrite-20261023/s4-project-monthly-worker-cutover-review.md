# S4 项目／月度旧工作簿 worker 切换屏障

两条旧入口 `_validate_project_workbook_in_worker` 与 `_validate_in_worker` 创建独立 Session 后，原来只由 adapter 初始化观察模式；缓存的 legacy 状态不能防止解析或等待期间首次切换。新入口在任何身份、上传 bytes、业务数据读取及领域锁之前调用共享 `runtime.guard_legacy_execution`。资源分别为 `maintenance_project_roundtrip`、`maintenance_manager_workbook`。canonical 请求使用其他带 VerifiedIdentity 的 facade/worker，不进入这些旧入口。

项目工作簿的机会式 retention cleanup 会独立 commit/rollback。保留该事务边界以避免把旧 validation 行锁带入后续 state 锁序；先 guard 保护 cleanup，再在其结束后重新 guard，第二个共享事务锁一直持有到真实校验 commit/rollback。若切换恰好发生在这两个事务之间，第二次检查拒绝。没有在生产添加 FakeSession 或 implicit identity 例外。

`test_authz_s4_project_monthly_worker_cutover.py` 使用真实 PostgreSQL、签名 XLSX 和原 adapter.validate：

- 原入口 6 failed / 2 passed（`/tmp/itsp-s4-project-monthly-worker-red.log`）：已切换仍先碰上传文件/数据库、首次 activation 越过尚未提交的校验。
- 修后原八例通过；新增 cleanup 成功提交及 SQL 错误回滚的两种事务间隙切换，最终 10 passed（`/tmp/itsp-s4-project-monthly-worker-green.log`）。
- 两个并发例以 `pg_blocking_pids` 核实 activation 等待真实 worker PID；等待时另连接看不到校验批次或 marker，原 worker 提交后 activation 才完成。
- 已切换和撤回配置均返回 `503 AUTHZ_LEGACY_EXECUTION_RETIRED`，不读 SysUser／领域数据、不打开已删除的上传文件、零新批次。
- 正常 never-cutover 校验保留可应用结果和真实批次。

旧 `test_validate_worker_owns_session_and_runs_outside_event_loop` 专测线程／Session 归属，显式 stub 新 SQL guard 并增加两次同 worker 线程调用顺序断言；其原 commit/close、数据版本、线程断言均保留。它不作为授权或并发安全证据。

这是作者红绿证据；外部独立复核与阶段精确 CI 另行记录。

最终作者组合验证：新增 10 例 + S3 canonical 项目／月度两文件 + 旧项目／月度 API 两文件，**61 passed**，日志 `/tmp/itsp-s4-project-monthly-worker-combined.log`。最终实现是两次 guard 保留原 cleanup 事务，不采用 savepoint 延长清理锁。

外部独立复核已闭合：backend_surface_inventory 原样 10 passed，`/tmp/itsp-s4-project-monthly-cross-review.log`；静态确认 cleanup commit/rollback 后第二次 guard，原清理事务释放语义保持。未改作者测试断言。
