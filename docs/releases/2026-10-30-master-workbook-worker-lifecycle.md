# 项目总表请求调度补充（2026-10-30）

决策: 无。技术修复沿用 D-03 权限、D-38 首次完整回执和 D-0042 合单协议，不新增业务口径、迁移或生产业务数据写入。

## 问题与范围

`master-workbook/validate`、`master-workbook/apply` 是 async 路由，读取上传后却直接执行同步 Excel/SQLAlchemy 服务。同步 SQL 等待 PostgreSQL advisory lock 时，会阻塞该 ASGI 事件循环；即使健康检查使用独立会话，也可能无法及时被调度。生产 2026-10-30 09:53:56 UTC 的 apply 504 耗时约 31.186 秒；同一窄时间窗口 PostgreSQL 有 statement timeout，紧邻语句分类为 `pg_advisory_xact_lock`。二者高度对应，但没有 request ID → backend PID 直接绑定，不把时间相关性表述为完整调用链证明。

本补丁只将这两个路由上传解析后的完整同步段移入受管 AnyIO 工作线程。V1/V2 服务代码、锁顺序、写前重验、合单确认凭据、首次回执重放、权限和错误映射保持原样。现有请求 `SET LOCAL statement_timeout = 30000ms` 继续生效；不会把单次锁争用的 504 改成成功或放宽限时。

## Session 与取消生命周期

1. FastAPI 依赖执行完、上传与缓存表单读取完，才把请求 Session 交给一个工作线程。路由在等待期间不再使用这个 Session；同一操作内的计划、重验、apply、业务回滚及审计均串行执行。
2. 复用 `_wait_for_roundtrip_task_terminal`，以 `asyncio.shield` 等待真实线程终态。普通请求取消或多次取消都先等待线程退出，之后才重抛取消，使 `get_db` 的 finally 不会提前 close 正在使用的 Session。
3. 未取消时，原始数据库异常继续传回原 `get_db`，由原逻辑 rollback 并映射 SQLSTATE 57014 为 504；取消后线程失败仍保留异常链，Session 关闭释放事务。没有独立后台任务或提前返回的成功回执。
4. 请求断开不能撤回已经启动的同步事务；它仍可能按原语义完成并提交。客户端重试须使用现有同文件、同项目、同实名操作者幂等协议。操作系统强杀进程不属于 Python 请求取消保证。

## 开发验证

仅在 `jinchen-dev` 的专用目录 `/home/cloudlay/work/workbook-eventloop-01a188b2` 与专用 PostgreSQL 15.19 容器 `itsp-workbook-eventloop-01a188b2`（仅 `127.0.0.1:32822`）运行；测试框架为每次 pytest 创建独占数据库和原始文件目录。没有连接生产或修改他人容器。

- 修复前精确基线 `7b3b92dde7ab3815e549705e79c4361e09c3ab3b`：实际路由 + 受控阻塞服务 + 同事件循环 `/probe`，V1/V2 validate/apply 四例全部复现阻塞，4 failed，实际 exit 1，11.32 秒。
- 修复后首轮：四个探针用例与八个重复取消生命周期用例，12 passed，实际 exit 0，10.69 秒。取消覆盖线程成功/失败，三次请求 Task.cancel，且 Session 只能在线程终态后清理。
- 新增真实 PostgreSQL advisory lock 等待用例：独立连接持锁，读取 `pg_stat_activity` 确认 `Lock/advisory`，在等待期间请求探针；保留真实 get_db/psycopg 路径，测试独占会话将超时缩为 1 秒，验证 504、重复取消、连接恢复 idle 且无事务。生产超时配置不变。
- 首轮新增 PG 夹具曾错传测试替身 Session，留下 `194 passed / 2 failed`，修正后重跑。后续扩展到八个相关文件的同轮回归出现 `292 passed / 18 errors`：专属 PG 容器初始 512 MiB 限额触发 OOM（完整 CID `1ff33e1b0b4eb0c664c3e2ee83ddce6df852de9dde63ba1124606928041f7579`，owner 标签核验通过，`memory.events` 的 `oom_kill=1`，10:23:29 UTC PG 子进程 signal 9 后进入恢复）。只把这只测试容器限额调为 1536 MiB，原始失败和日志保留，按原八文件重跑；不将环境失败计为通过。
- 开发机 AST 比较通过：新 worker body 与基线原同步 body 相同，只有缓存表单中的 `merge_token` 读取移到 worker 交接前。API 源码 SHA256 从 `815ae5d6e7dce4fc2d42a51eb5209386a6490b7854561d994b3d751f97876f23` 变为 `24bbef5c66e85ea4ccdb9a1a256abda4ea154d1681079d8800d93077b4b236e2`；最终生命周期测试文件 SHA256 为 `4eaf25f1541333eb1254d21145a58a22ed4cb6ca2317a27470ade4d2d8194779`，本地与开发机一致。
- 最终八文件同轮回归：**310 passed，实际 exit 0，351.82 秒**；OOM 计数保持 1，没有新增。文件为 `test_master_workbook_worker_lifecycle.py`、`test_maintenance_project_master_workbook.py`、`test_maintenance_project_workbook_v2.py`、`test_master_workbook_stale_replay.py`、`test_master_workbook_issue_no_batch.py`、`test_maintenance_master_workbook_validation_errors.py`、`test_maintenance_site_workbook_reauthorization.py`、`test_maintenance_project_master_v2_editable.py`。其中包含真实锁后撤权、整本回滚、合单凭据、回执重放、V1/V2 既有契约；这是定向回归，不替代最终精确提交的完整 CI。原始结果保留在专用目录 `green-resource-fixed.log`，主会话本地证据目录另存 `workbook-eventloop-green-final.log`。

## 发布与观察

该补丁未自行 push、合并或部署。由主会话统一执行跨模型复审、精确提交 CI、正常合并和既定发布流程。前后端仍绑定同一发布提交，生产写入仍需既定备份与回滚步骤。

发布后只读观察业务 503/504、连接池占用、idle in transaction、主工作簿请求耗时以及独立轻量探针延迟。若同一工作簿仍因真实锁竞争返回 504，继续查持锁者及事务时长；不通过删锁、增加超时、取消权限或版本校验来掩盖争用。线程池容量和 PostgreSQL 连接池仍是有限资源，本修复不是无限并发或整套系统吞吐量保证。
