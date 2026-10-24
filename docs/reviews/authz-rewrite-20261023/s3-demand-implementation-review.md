# S3 需求单 13 入口实施与验证

状态：作者实现冻结，实际 PostgreSQL + Cerbos 新旧组合 100 passed；最终新增目标逃逸与独立 void 动作反例后，最新 20 个新测试全部通过。独立交叉审查待回报。未提交、未合并、未部署；不代表 S3 阶段总体验收。

## 封闭入口

资源为 `maintenance_demand`，共享 facade 是 `backend/app/services/maintenance_demand_authorization.py`。API 在该资源实际 cutover 后使用 canonical；legacy 模式继续原页、动作、管理员及业务范围。接口未添加角色旁路。

| 实际入口（前缀 `/api/maintenance/demands`） | 数据动作 | 真实载体 |
| --- | --- | --- |
| POST `/search` | read | 固定 purpose 的集合 PlanResources，SQL 前过滤再分页/计数 |
| GET `/orders/{source_order_id}/lines` | read | actual source 与实际项目父关系 |
| GET `/manual-protection-review` | read_manual_protection | 固定 purpose 集合，原全部父项目范围过滤 |
| POST `/lines` | create_line | 输入项目及稳定 actor/key 命名的新建或既有实际 source |
| PATCH `/lines/{raw_line_id}` | edit_line | actual line→source→项目；updates 整包 server SHA |
| POST `/lines/{raw_line_id}/clear-override` | clear_override | actual line→source→项目；原 field/digest |
| POST `/delete-intents` | void_plan | 真实完整 source 集合与 operation_input，既有计划还核验历史父关系 |
| GET `/delete-intents/{intent_id}` | read_delete_intent | stable owner 的实际 intent，历史及当前完整父关系 |
| POST `/delete-intents/{intent_id}/arm` | void_plan | 实际 intent 与原 selection digest |
| POST `/delete-intents/{intent_id}/cancel` | void_plan | 实际 intent 与原 selection digest |
| POST `/delete-intents/{intent_id}/execute` | void | 实际 intent、冻结 items 和当前全部父关系 |
| POST `/void-fast` | void | 首次完整 operation_input；既有 stable owner/key 命中才用 registry `replay/context_receipt` |
| POST `/{source_order_id}/restore` | restore | 实际 source + tombstone/历史项目，不恢复已停用挂靠 |

上述集合与 runtime `ENTRY_CONTRACTS` 对应 13 个精确 method/path；无通用权限探测 endpoint。每条同路径 `view=authorization` 仅查询身份、关系、版本、实际选中 ID 和输入摘要，返回 locator/context，不读展示事实、不造 intent、job 或审计。所有正常读取在同一 RR 观察中核验 precondition 并物化 payload。

## 关键边界

- 搜索联系人只使用本资源可选 `data_customer`；不把字段变成 read 硬依赖，不借其他维保资源字段。删除计划快照继续不包含客户联系信息。
- 所有对象从 DB 的实际 child→source→parent 推导。未归属 source 用真实 false 关系 subject，仅 all_projects 合同可覆盖；不把空集合当全局允许。历史 intent、当前重新挂靠和墓碑历史父关系均进入完整目标集。
- 命令先做 RR 预检，按原业务的 idempotency/global/state/source/assignment/line 锁序取得完整领域锁，再做锁后观察；复杂原语暂存后才锁当前 actor 并重新核验全目标 PDP，检查前后真实父集合没有逃出原集合，任何失败整体 rollback。业务自身的 version bump 不造成假 stale。
- 原删除状态机保留 reviewed/armed_wait/7 秒等待/过期/取消/终态冲突；只有最终当前授权成功，才允许提交原有 durable OCC conflict 证据。409 不重放。
- Intent 实际 owner 是 stable user ID；改名继续可用、用户名复用和历史 NULL 不可认领。手工行创建的幂等命名改为独立 stable actor/key 命名空间，合成批次记录 owner 并验证；原行仍从当前实际事实重读，不造历史响应副本。
- `Intent.result_json` 是历史回执，使用真实 persistent intent artifact binding。首次 void-fast 的临时 operation_input 不存 provenance；原语完成后重新解析 registry 的实际 receipt 分支。read 与 replay 两种 format 分别要求 `read_delete_intent` 和原 void 动作，发布 read format 由原 producer 动作授权，不强加读动作。每次消费检查完整内容 hash、stable producer、真实历史/当前父关系和当前对应合同。
- 共享内部 `void_fast` 只接受 exact typed `SourceCancellationAuthorization` 或 `WbddReconciliationAuthorization` 的同 DB/事务、已 capture、实际 source/parent 子集证明。授权与最终 actor 锁留给完整外层命令；不追加 demand Grant，不收 JSON 布尔 permit。未迁业务原默认纯领域调用继续兼容。

## 验证记录

使用隔离 PostgreSQL 测试库与实际 Cerbos binary；无生产数据。

- 原需求单 5 文件 82 passed：`/tmp/itsp-s3-demand-legacy-first.log`。
- 首轮 17 个新增真实 API/锁等待对抗用例通过：`/tmp/itsp-s3-demand-adversarial-second.log`。
- 18 新增 + 82 原有组合 100 passed：`/tmp/itsp-s3-demand-combined.log`。
- 最终 20 个新增测试 passed（43.34s）：`/tmp/itsp-s3-demand-final.log`；包含实际新 source 新父越界的事务回滚，以及仅 void / 仅 read_delete_intent 两向独立授权。
- 最新实际新建 source 闭包修订正向 1 passed：`/tmp/itsp-s3-demand-create-actual-parent.log`。
- 最新 registry 生成物：generator `--check`、frontend tsc 通过，context 20 + DemandAuthorization 10 共 30 passed：`/tmp/itsp-s3-demand-generator-frontend.log`。

反例覆盖所有 13 个入口独立拒绝与纯解析视图、字段遮蔽、scoped SQL 计数/分页、批量全目标、实际项目锁等待后撤权/移父、变化输入 precondition、历史计划原/新父、稳定 owner/历史 NULL、真实 manifest 发布后异常回滚、manifest hash 投毒、终态 OCC、typed permit 伪造和 actual owner 阻断有损 downgrade。完整 S3 全量 CI、真实浏览器和最终统一验收仍由阶段门确认。
