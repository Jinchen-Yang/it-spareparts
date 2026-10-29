# 单 PN 目录投递候选运行手册

决策: 无（D-36 自动发布边界内的技术实现；创建即触发已确认）。

本文件说明候选能力与统一发布步骤，不表示已合并、已启用或已上线。D-0048《售前发布接管与保守收口》及现有交接已授权01a188b2主会话统一集成发布，无需再次向用户请求发布授权；本隔离性能线程仅交付候选PR，不执行合并或部署。配对候选为[ITSP #494](https://github.com/Jinchen-Yang/it-spareparts/pull/494)与[商城 #86](https://github.com/Jinchen-Yang/spareparts-shop/pull/86)，保持Draft，交总监督最终复核后转Ready并交接管主会话。初始隔离基线没有携入售前未提交改动；由接管主会话先将#492合入main，再严格同步main并合流迁移为单head，属于已授权的正式集成，不是携入未批准实验。实际生产仍以发布标签为准，不引入test分支实验。

## 能力与边界

普通主数据建档、售前原子建档绑定、shop 主数据集成建档共用 `create_part_in_transaction`；新 PN 与 `catalog_delivery` 在同一事务提交。任何失败回滚均不留下任务。已有身份查重、重放不会再创建任务。创建响应只表示主数据已创建，不表示商城已应用。

消费者按稳定 part_id 通知商城重新读取当前源资料，保持原身份、墓碑及直接父节点；不随负载传递价格、销售计数、库存或业务状态。只对共用人工建档入口新增通知，不为存量身份或 ETL 导入自动造任务；既有定时全量/增量同步继续兜底。人工冲突保留 `conflict`，不得报告已成功发布，也不自动覆盖商城人工状态或发起 AI。

## 配置和迁移

默认 `SHOP_CATALOG_SYNC_ENABLED=false`。即使关闭，创建仍保存待投递记录，重新启用后可恢复消费；默认不开外网请求。

| 配置 | 作用 |
| --- | --- |
| `SHOP_CATALOG_SYNC_ENABLED` | 显式开启消费者 |
| `SHOP_CATALOG_SYNC_URL` | 完整 `https://<shop-host>/api/integrations/catalog/parts/sync` 地址 |
| `SHOP_CATALOG_SYNC_TOKEN` | 独立32–256位可打印ASCII Bearer凭据，只存在后端安全配置 |
| `RESALE_CATALOG_TOKEN_SHA256` | 既有目录只读服务摘要，继续用于商城读取 `/by-ids` |

通知URL禁止账号密码、查询串、fragment、其他路径和重定向；生产只接受HTTPS，开发仅回环地址可用HTTP。通知凭据不得复用图库令牌。不得把令牌、真实环境文件或响应正文放入命令、日志或仓库。

由01a188b2接管主会话按D-0048及已有授权统一发布：先完成#492进入main后的严格同步、迁移单head合流和联合验证，再备份并记录恢复点；先部署支持v1通知的商城接收端及其持久回执迁移；再部署itsp的前后端同提交候选，先 `alembic upgrade head` 再启动应用；确认两个目录服务凭据分别匹配，最后单独启用消费者。按仓库流程记录精确 `release-` 标签和用户点按验收。本隔离性能线程不执行这些发布操作，也不为接管主会话增加新的用户授权门。

itsp 迁移 `c6e8a1b3d5f7` 只新增技术出站表及两个部分索引，不改业务数据。降级会删除全部投递回执和积压任务：常规回滚优先关闭消费者、保留表与旧应用兼容；必须降级时先停全部生产者/消费者并备份出站表，确认已应用与待核状态后再按上一revision降级。重新升级不会自动补回丢失任务，需恢复备份或由既有批量同步兜底，禁止把未投递当成功。

## 配对接口 v1

`POST /api/integrations/resale/parts/by-ids` 使用既有独立 catalog 只读认证。请求 `{"partIds":[123]}`，1–100个严格正整数，最大为PostgreSQL int，不接受额外键；重复ID折叠、缺失ID省略。响应 `{"schemaVersion":1,"items":[...]}` 与 `/list` 共用唯一白名单函数；墓碑保留请求的原partId，不转换成最终规范身份。该端点不携带销售计数、价格或业务数据，返回 `Cache-Control: no-store`。

通知 `POST /api/integrations/catalog/parts/sync` 使用独立 Bearer。请求 `{"schemaVersion":1,"deliveryId":"UUID","partId":123}`。只有严格匹配的HTTP200、schemaVersion、deliveryId、partId及 `applied|unchanged|conflict` 才是有效回执；额外键、类型错误、错身份、未知状态均视为可重试失败。响应总量最多4KiB，总网络期限15秒，不跟随redirect，不读取代理环境配置。商城须保证deliveryId+partId回执与应用同事务、与定时同步共用锁，并保护人工状态。

## 恢复和诊断

每个API进程启用一个消费者；PostgreSQL `FOR UPDATE SKIP LOCKED` 逐项领取，60秒租约，领取和回执事务各自有5秒SQL/1秒锁等待预算。网络期间不持有数据库连接/事务。提交只唤醒消费者，最长5秒轮询补偿跨进程唤醒遗漏；应用重启恢复持久任务。

至少一次投递：断网、错误HTTP、错回执和超时指数退避5秒起至1小时，不有限次静默丢弃。过期租约可重领，每次产生新lease token；旧消费者和过期回执CAS失败，不得覆盖新状态。进程退出等当前有界请求结束并回执；强制中断的未回执任务在租约到期后恢复。同deliveryId重试须依赖商城幂等回执，不得重新建品或产生库存流水。

有主数据页面权限的实名用户可只读查询 `GET /api/parts/master/{part_id}/catalog-sync`，没有放宽原权限。接口沿相邻主数据读面使用硬鉴权，即使 `ENABLE_RBAC=false` 也拒绝匿名、无效或被撤销身份；RBAC开启时另校验页面权限。`enabled=false`与`pending`可同时出现，表示已排队但投递关闭；`not_queued`仅说明没有本能力创建的任务，不证明商城缺失。`delivering`是尝试中；`succeeded`附带`applied|unchanged`；`conflict`是需人工核对。状态附尝试次数、时间和安全错误码，不返回URL、凭据、lease token或原响应。

安全错误码为 `transport_error`、`deadline_exceeded`、`remote_http_error`、`invalid_receipt`、`manual_conflict`。运行日志只记录结果码和CAS是否接受，数据库异常只记 `worker_error`，不记录异常堆栈、SQL、参数、令牌、PN原文或业务响应。人工冲突处理沿用现有人工目录流程，本候选不提供改业务状态或强制重放的写接口。

## 验证与测量口径

仅在指定开发机、专属合成数据库运行 `test_catalog_delivery.py` 以及既有建档/目录回归，验证三入口提交与失败回滚、租约过期/并发CAS、网络失败和恢复、严格配对契约、只读权限及升降升迁移。与商城测试合并核对不变的全量游标、人工保护、无库存流水/AI任务。测试结果写本轮性能报告。

旧生产扫描等待与新合成端到端投递时长必须分别标注。当前没有新链路生产实测；候选本地通过不能称生产已提速，也不代替用户安排的实际点按验收。

真实双服务验证脚本为 `scripts/verify_pn_delivery_e2e.py`，只在授权的 Linux 开发机执行；必须显式传入专属合成库连接变量 `PN_E2E_DATABASE_BASE_URL`，固定校验回环端口32774与测试基库名。脚本自行创建两个带唯一归属标记的数据库、任务临时TLS证书、两个附着子进程与回环转发器，完成后停止子进程、删除自己创建的库及临时文件，不删除共享容器。环境只传合成凭据与显式配置，不读取已有 `.env`；输出仅聚合计时和清理结果。

2026-10-29该脚本空闲复测完成1个通知延迟屏障热身和20个正常样本，验证建档不等待远端、真实TLS `/by-ids`、持久回执重放和ID碰撞拒绝；21PN/42零库存SKU、库存流水与AI任务均为0，完整批量游标行未变。计时名称为“建档HTTP响应”“响应到观察成功”“请求开始到观察成功”，20毫秒轮询；未测精确数据库commit时点。数值、候选身份和CI结论统一记在[性能报告](../performance/2026-10-29-refresh-latency.md)，不以合成测试冒充生产效果。
