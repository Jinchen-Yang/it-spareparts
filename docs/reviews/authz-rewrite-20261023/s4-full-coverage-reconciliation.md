# 全阶段授权迁移覆盖对账（当前工作树）

目前可以证明实际入口与执行词表闭合，不能据此宣称统一验收完成。对账发现的后台跨切换缺口已完成本地修订及跨作者复核；精确提交 CI、浏览器及统一验收仍未完成。生产未操作。

`verify_s4_full_coverage.py` 直接加载真实 main 路由、可执行目录、精确上下文 registry、平台 boundary 与旧阶段逐入口账。它不启动 lifespan、不连接业务库、不运行测试、不使用文档来赋权。生成物 `s4-full-coverage-reconciliation.json` 是审计证据，约 1.4 MB，应用不导入它。

## 分母与边界

| 对账单位 | 当前数量 | 解释 |
| --- | ---: | --- |
| 实际 HTTP method/path | 398 | S2 89、S3 239、S4 Import 9、Agent 13、平台 48，精确集合相等 |
| HTTP 权威分类 | 398 | 普通资源 339；平台边界 45；退役 12；固定业务拒绝 1；资料/安全 body 分支共用 PUT 1 |
| 员工可执行资源 | 107 | 无 planned；不能把控制/机器端点算作可分配员工 Grant |
| 退役资源描述 | 5 | 空动作/无 surface；旧 alias 明确替代，不复活旧授权入口 |
| 平台边界描述 | 10 | 真实 named-admin/auth/public/service/framework 等固定边界 |
| resource/action 配对 | 553 | 包含 display；业务 action 与 show 仍分离 |
| resource/field 配对 | 159 | 字段只在各自 ResourceAccess 内有效，数量不代表组合测试覆盖 |
| surface | 222 | 155 route_entry、54 embedded、13 dormant；休眠面不算已验证可达 UI |
| 新登记非 HTTP 合同 | 3 | 2 个 TOOL generate + 1 个 WORKER import.execute_item |
| 实际 Agent 工具 | 18 | 分别列出真实受保护 facade、文件来源及静态技能工具 |
| 实际后台执行适配器 | 14 | 独立列出所有领域 worker、共享线程器、输入解析、发布重验、失败审计、临时文件生命周期 |
| 线程/任务/线程池调用位置 | 36 | AST 扫描 backend/app；不是 36 种业务权限 |
| 本地构建/维护/运维脚本 | 25 | 按仓库脚本路径单列；部分使用直接数据库操作者边界。未检查或更改主机 cron/systemd，不声称约束拥有数据库凭据的运维人员 |

398 包含框架内置方法、旧退役端点和 D-05 固定拒绝，未用路径前缀排除。`POST /api/maintenance/projects/stable` canonical 固定拒绝，不能虚构 create Grant。账户资料与 security_change 同一路径按实际 body 选择不同合同。

## 后台执行与切换反例

精确逐项记录见 `s4-background-entry-reconciliation.json`，其中每项有真实启动函数、复用/独立合同、重验位置、legacy 剩余用途与持久测试文件。自动启动点扫描覆盖 `Thread`、`create_task`、线程池、`BackgroundTasks` 及共享解析/worker helper；新出现未分类文件会使校验失败。

已发现并修订的真实问题：

1. 返还导入 public `run_job` 在转发 protected worker 前全 ORM 读取 report。独立真实 PG 两参数（epoch/Grant 撤销）先 **2 failed**，改为仅 ID/owner/protocol 读取后原样 **2 passed**。持久证据 `test_authz_s4_return_worker_independent.py`，日志 `/tmp/itsp-s4-return-worker-independent-{red,green}.log`。内层测试通过不能替代外层 dispatcher 检查。
2. 旧通用 import worker 可在源 cutover 后提交真实采购数据。独立原入口 **1 failed** 后增加资源级共享事务锁与首次 activation 独占锁；旧 worker 按实际输入来源在每文件事务首重新进入。新增真实 PG 验证 activation 确实被业务连接阻塞到 commit、完成切换后旧任务拒绝、撤配置仍拒绝、其他 never-cutover 来源兼容及未知来源保守拒绝。持久证据 `test_authz_s4_legacy_worker_cutover.py` **4 passed**；与原 import32/migration13 的前3组合 **48 passed**。不能把提交时点的旧许可永久带入异步队列。

同型仓库、全局 roundtrip、返还 worker 多事务及项目/月度旧工作簿线程均接入同一资源 guard。仓库原4反例外部复跑通过；全局/返还独立组合13通过；项目/月度原10反例外部复跑通过（`test_authz_s4_project_monthly_worker_cutover.py`，`/tmp/itsp-s4-project-monthly-cross-review.log`），其中项目 cleanup 的 commit/rollback 后再次 guard，保留原事务释放与业务锁序。Root 最后 worker 组合82通过（`/tmp/itsp-s4-root-worker-cutover-final.log`）。资源锁顺序为 cutover guard → 领域锁 → 用户锁，历史 RR 快照不可用于 guard。

Agent 的旧权威判定本来就是全局 `canonical_management_required`，只锁 Agent 资源会漏掉其他资源或 artifact protocol 首次切换。真实 checkpoint 原反例及文件碰撞/写盘失败/首 marker 窗口三反例已闭合：旧会话短写与文件发布先取全局 transition 共享事务锁；任一首次 resource/protocol marker 先取匹配独占锁，再按资源排序锁。文件渲染在锁外，锁内以独占创建与完整临时 metadata 硬链接发布，失败只清理本次创建路径，禁止覆盖旧同 ID 文件。没有把 LLM 或文件格式化包在长事务里；普通不同资源 legacy worker 仍只锁自身资源。`test_authz_s4_agent_cutover_independent.py` 3 项与 `test_authz_s4_legacy_file_publication_independent.py` 原3项、相关历史测试本地组合16通过；扩组合57通过；Astra 外部组合28通过（`/tmp/itsp-s4-agent-global-barrier-independent.log`）。并发测试用实际 `pg_blocking_pids` 证明提交先于 activation，不以同线程等待伪称生产死锁。进程环境配置改变不是数据库事件，需部署时停止旧进程，此处不承诺原子保证。

守护线程仍是进程内执行。这里没有实现或声称进程崩溃自动续跑、持久外部队列或跨实例恰好一次。

## 证据层级与尚待验收

JSON 对每个入口保留源码 handler、精确合同、旧阶段语义账和 `legacy_residual_disposition`。S2/S3 既有精确 CI 是历史证据，不能被静态扫描重命名为当前树测试通过。S4 import 已有作者 45 项与 root 外部同 45 项；平台独立新增 8 项和作者组合 58 项。Astra 原先的平台两个反例另存 `test_authz_s4_platform_astra_independent.py`，不与这 8 项混称同一作者。Agent 与公共抽取的独立结论使用其独立报告。

仍须明确完成：

- 当前树最终定向组合与精确提交完整远端 CI；本地后台切换反例及跨作者复核已收齐。
- 真实浏览器对各可达页面的允许/拒绝、独立块、CAS、撤权、跨身份/对象以及下载/聊天重放验证；静态 surface 闭合不代替浏览器。
- HTTP、Agent、后台及生成文件复用同一实际资源/字段/对象合同的端到端验收。
- 迁移/拒绝降级和运维恢复演练、代码成本与残留旧路径审查、用户统一验收。可合并与可生产分别裁定。

最终隔离库 `authz_final_browser` 已通过正常 API 准备五源合成 XLSX、诊断采购 report/errors、mixed job、作废预演样本。文件位于 `/tmp/itsp-final-acceptance/import-fixtures`，安全索引在同目录上级 `facts.json`，口令仍独立 0600 文件且未写入报告。这只是验收数据准备，不是浏览器通过结论。

## 兼容成本与清理处置

never-cutover 旧 handler/adapter 的保留具有明确用途：未切资源继续原协议，持久 marker 一旦存在就不能因配置撤回重开。规范模式旧 bool 写入整体拒绝；`/accounts/_meta` 明确 canonical 410；已退休业务端点保留可观察 410/固定拒绝。控制/public/machine 边界不改造成员工 Grant。旧纯计算、输入解析和审计 helper 若仍被受保护 facade 调用，属于业务复用，不是第二套权限真值。

本次变更是大规模架构与业务接缝迁移，不能称“代码大减”或“只加一层薄 adapter”。最终变更量应在精确候选树统计应用代码、测试、生成目录/策略与审计文档，包含未跟踪新增文件；精确成本另由 root 的候选树统计记录，不以旧 diff 数值冒充最终成本。约 1.4 MB 对账 JSON 和大执行 spec 是审计数据，未复制进运行路径。
