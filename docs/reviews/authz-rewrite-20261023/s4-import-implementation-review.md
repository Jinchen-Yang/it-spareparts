# S4 通用导入实施与验证记录

S3 精确提交 `c0396fc08f6d5084358ebf352ffedad717980274` 全量 CI 通过后开始本包。当前为本地实施证据；生产未变更，S4 精确提交的全量 CI、跨作者最终审查和统一浏览器验收仍是独立门槛。

## 实际执行链

九个 HTTP 入口继续使用既有 method/path。`source_import_access` 是唯一受保护应用服务，HTTP 与 daemon worker 不相互导入。五种来源由实际工作簿识别和选中工作表决定，既有内容预检 v2 不掺入授权错误。每个来源独立 precheck / submit_skip / submit_upsert / read_status / read_report / export_errors；作废预演是独立 `import_expense_void.preview`，不借提交动作。

| 实际入口 | 应用服务 | 已入仓语义证据 |
| --- | --- | --- |
| POST /api/import/precheck | precheck：完整实际输入绑定；未知格式只输入诊断 | `test_authz_s4_import.py` unknown-precheck、独立submit、真实wire |
| POST /api/import/upload | submit / _execute_file：真实 source permit，单文件原事务 | 同文件五来源参数化、撤权零事实、输入CAS、计划两个毒针、作废金额漂移 |
| POST /api/import/upload-batch | submit：完整逐文件 manifest，提交后入队 | 同文件mixed-source全称零job、初始/后续manifest篡改、真实worker |
| POST /api/import/expense-void-preview | void_preview：可选 K，SQL 金额承诺 | 同文件无K不选择金额、v2执行与金额变化拒绝 |
| GET /api/import/jobs | read：来源 SQL 筛选在 limit 前 | 同文件实际job wire与mixedjob受限投影 |
| GET /api/import/jobs/{job_id} | read：真实完整source集合+可见items | 同文件mixedjob不返回hidden filename、序号、全件状态/数量 |
| GET /api/import/batches | read：来源 SQL 筛选在 limit 前；all 不改 owner-only | 同文件跨上传人合法历史及另一来源排除 |
| GET /api/import/batches/{batch_id} | read：status/report独立块、字段同资源 | 同文件status-only零report事实查询、report-only独立使用 |
| GET /api/import/batches/{batch_id}/errors.csv | read：完整同RR spool，发布当前重验 | 同文件audit后第二连接撤epoch，首字节前401 |
| WORKER import.execute_item（非HTTP） | process_job / _execute_file | 每文件stable owner+enqueue epoch；前文件保留、后文件撤权拒绝；完整plan锁 |

表中列的是持久测试文件里的具体反例类别，并不宣称每个状态都已有浏览器覆盖。真实 HTTP DTO 输出见 `s4-import-wire-real-http.json`；由 `test_real_http_wire_payloads_match_fixed_contract` 生成，只有 HMAC token 换成固定脱敏值，其他结构/内容来自真实 PG/PDP/HTTP。

## SQL、写事务与 worker

- history 不是 owner-only。来源 Grant 的可见集合进入 SQL 后才排序/limit。未知历史 protocol/source/owner 明确 409；不通过旧 type 猜来源。status 不加载 report/errors 事实；报告和错误导出须同资源完整 C/S/K/F。这里收紧旧报告未遮罩缺口，不把 page_import 当全字段既得授权。
- mixed job 保留可见 items，restricted/partial 明示；隐藏文件存在时顶层 count/status/note/finished_at 和 input_index 为 null。未查询隐藏文件内容，不能以 0/未开始伪装受限。
- `SourceImportPermit` 来自真实 ResourceAccess，绑定 Session、活跃 transaction、source/hash/mode 与完整转换计划摘要。loader 前、来源派生前和完成前复核实际计划；失败事务、跨事务或输入变化都不能继续写。原 parse/业务规则仍在 ETL 纯层。
- 顺序为全局 DATA_CHANGE advisory → job及完整排序item锁 → 既有来源/领域锁 → 最终 actor SHARE与当前Grant/PDP → 同事务回执投影/commit。actor 没有前置共享锁再升级。每个文件独立提交；撤权不撤销先前已完成文件，也不允许后续文件继续写。
- 每文件开始和最终提交都核整份 immutable manifest；真实两连接测试确认后续 item 更新阻塞到当前文件提交，之后摘要变化使后续文件拒绝。任务 error code 为闭合枚举，不复用旧 note/异常文本。
- 成功回执不附报告/敏感统计。保留的失败回执返回 `commit_state=receipt_only`；无提交失败为 none。失败回执只取本次 permit 绑定对象，不查相同 hash 的历史最新记录。
- rollback 不删除共享 hash-addressed 正式归档，以免删除等待同全局锁的另一导入引用的文件。仅清理本次私有临时文件；既有带年龄/引用检查的归档 GC 负责孤儿清理。
- profit/inventory/master 的既有提交后派生刷新仍是服务端后置工作，不附送 Grant；只返回 complete/failed，不返回计算统计。maintenance_cost 保留源事务内原子执行。
- 作废 v2 HMAC 绑定 SQL 生成的金额 SHA 承诺；没有 K 时不读/返回金额，仍能发现预演后金额改变。legacy v1 保持原协议。

## 共享实施

`platform_definitions`、catalog/manifest、context registry、policy generator、`authz_s4` protocol marker、模型与唯一迁移 `e4a7c9f2b6d8` 已落。11 个 S4 员工资源闭合；机器/管理/public 位于独立 boundary registry，不可赋普通 Grant。self 策略明确 `R.attr.is_owner`。迁移使用冻结声明、来源资格与回滚 receipt；保留 inactive entitlement，canonical none/显式空拒绝不补权。

新增 SysImportJob 稳定owner/epoch/generation/digest、SysImportJobItem 规范逐文件输入/状态、batch protocol/source。既有owner列与约束复用，无第二迁移。downgrade 拒绝持久切换、规范作业/产物或不可证明回滚的授权变化；未放宽已有S1/S2/S3保护。

## 已运行证据

- 32 个新导入 HTTP/PG/PDP/worker 测试 + 13 个共享真实迁移/目录/种子/降级测试：**45 passed**，`/tmp/itsp-s4-import-shared-final.log`。持久文件 `backend/tests/test_authz_s4_import.py`、`test_authz_s4_migration.py`。
- Root 外部原样执行这 45 项亦通过：`/tmp/itsp-s4-root-import-final.log`；这是外部复跑，未冒充其新增独立向量。
- 更早的首18新测试+既有预检/作废/CSV四文件组合：**139 passed**，`/tmp/itsp-s4-import-full-first.log`。这不是最后修订后的全量CI；后续新增permit/锁修订由上面的45项覆盖。
- 旧 worker 未绑定 `exc` 问题：使用 HEAD 的原函数 AST、同一真实 PG 两文件测试，**1 failed**，`/tmp/itsp-s4-legacy-integrity-red.log`；现实现仅补 `except ... as exc`，永久回归确认两文件均处理、两失败条目保留。未声称生产出现率或曾导致数据损坏。
- `test_import_error_download.py` 中模拟无 SQL能力的 failing-db单元测试改为直接调用明确 legacy CSV 原语，保留原失败清理断言；其他真实CSV/授权测试仍走正式入口。

## 必须保留的兼容与未完成验收

只有无 view、实际已识别来源全部 never-cutover 的正式请求可走原 legacy handler；任何 `view=authorization` 必须 canonical，未启用返回503，不能执行旧导入。每资源持久marker禁止撤回配置恢复旧允许。目录/混合/未知输入的 canonical 合同需要完整依赖；部署必须一组启用五来源，不能宣称任意partial配置下列表可用。

旧 daemon 只为 genuine never-cutover 作业保留；新作业只执行稳定owner+完整manifest路径。现有 daemon 仍非持久外部队列，本包没有宣称进程崩溃自动恢复或跨实例恰好一次。远端完整CI、跨作者新反例、真实前端消费与统一验收待根代理整合，不以此报告替代。

## 最后跨阶段 worker 修订

全后台对账发现“排队时 never-cutover”不足以证明执行时仍可用：真实旧 worker 在 purchase 已切换后仍提交采购 batch。新增 `test_authz_s4_legacy_worker_cutover.py` 原入口先 1 failed（`/tmp/itsp-s4-legacy-worker-cutover-red.log`），修订后 4 passed（`/tmp/itsp-s4-legacy-worker-cutover-final.log`）。每文件纯解析实际 source 后取该资源共享事务 advisory；first activation 对新 marker 取同资源独占锁，等旧业务事务完成才切换。锁键固定 namespace+SHA256，资源稳定排序；guard 不允许历史 RR 快照，持锁到原事务 commit/rollback。未知格式且任一相关资源切换即拒绝，不给未知源调用旧业务的机会；其他实际 never-cutover 来源保留兼容。

真实双连接 `pg_blocking_pids` 证明 activation 等待 legacy commit，完成后旧请求503，撤配置仍503。新增前三例与原 import32/migration13组合48 passed（`/tmp/itsp-s4-legacy-worker-lock-green.log`）；最后第四例又验证其他源与未知格式边界。Root 外部 worker 组合82项通过（`/tmp/itsp-s4-root-worker-cutover-final.log`），其中包含新增4项；精确CI仍单独等待。这次 guard 是实际执行期屏障，不将各业务域耦合到同一个全局切换锁。

## Agent 全局 authority 的最后共享接缝

仅 Agent 的真实 legacy 权威依赖全局 canonical-management 状态，因此新增单独 global transition barrier，首次 resource marker 与 artifact protocol marker 使用同键独占；Agent 短会话写和文件发布使用共享。每资源 Import/工作簿旧事务仍只使用各自资源锁，不将所有领域耦合。当前工作树的 Agent/ChatStore/协议/迁移/worker 定向扩组合57通过，外部 Agent 组合28通过；完整日志和持久反例见全覆盖对账。环境模式变更的进程生命周期不属于数据库原子性保证。
