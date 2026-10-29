# 单 PN 创建后目录投递技术方案

决策: 无（D-36 自动发布边界内的技术实现；用户已确认创建即触发）

测量：2026-10-29 H6Z27A 两端数据库创建记录相差4小时13分32秒，均落在6小时10分的全扫窗口；没有任务关联证据证明当次反馈的全部等待来自单次扫描。现有五分钟changes同步不创建未绑定身份，代码没有创建后独立投递路径。源码没有商城单身份同步接收接口，故采用两个独立候选PR，本隔离性能线程不合并、不部署。

配对为[ITSP #494](https://github.com/Jinchen-Yang/it-spareparts/pull/494)与[商城 #86](https://github.com/Jinchen-Yang/spareparts-shop/pull/86)，本线程保持Draft并只交付候选。D-0048及现有交接已授权01a188b2主会话统一集成发布，不需重新向用户请求授权；由其安排#492先合入main、性能候选严格同步main和迁移单head合流，再按运行手册统一发布。

## 配对契约 v1

1. itsp 共用 `create_part_in_transaction` 同事务记录持久 outbox；失败回滚不留任务，提交仅唤醒后台消费者，不在用户请求内做跨系统 HTTP。专用worker有租约、CAS回执、指数退避及重启恢复；状态和安全错误码可查询。
2. itsp 增加 `POST /api/integrations/resale/parts/by-ids`，沿用独立 catalog 只读服务认证，body `{"partIds":[正整数]}`（1–100项、禁止额外键），返回 `{"schemaVersion":1,"items":[...]}`。每项与既有 `/list` 的身份/字段白名单完全同形；保持原 part_id（不把墓碑重定向为另一身份）、不含销售计数/价格/库存/业务状态。
3. itsp 向 shop `POST /api/integrations/catalog/parts/sync` 投递 `{"schemaVersion":1,"deliveryId":"UUID","partId":正整数}`。使用独立目录同步 Bearer 服务凭据；不复用图库密钥或浏览器会话。默认关闭，由接管主会话按已获授权统筹配置密钥并启用，本隔离线程不改生产。
4. shop 认证后按 partId 拉取**当前** `/by-ids` 读面，复用现有目录应用逻辑、同一 advisory lock 和冲突保护；只应用目标身份，不推进/重置全量 cursor，不触发全扫，不排 AI，不产生库存流水。源缺失/读取失败返回可重试错误；人工冲突明确回执，不能报成功。
5. shop 用持久 deliveryId+partId 回执保证重放；相同ID不同身份拒绝。回执与目标应用同事务。持锁后若镜像版本新于拉取的源版本，跳过旧版本并回执 unchanged；批量路径也不得用较旧页覆盖新通知结果。人工覆盖/停用和已有图库/库存历史继续保护。
6. 成功响应：`{"schemaVersion":1,"deliveryId":"UUID","partId":正整数,"status":"applied|unchanged|conflict"}`。itsp校验所有身份字段，不把任意200当成成功；conflict保留待核状态，可观测，不写业务事实。
7. 小时/每日批量与既有cron保持兜底。outbox单独迁移、downgrade；接管主会话依运行手册先部署接收端、再部署生产者并单独启用，本隔离线程不执行。

## 验证

仅 jinchen-dev 合成数据：三建档入口事务回滚、提交不等网络、断网退避恢复、租约到期重领、并发消费者、旧回执CAS、重复投递、认证默认关闭、ID碰撞、源更新/合并、现有人工保护、无库存流水与AI任务、全量游标不变、两仓精确契约、升降升迁移。前后对比以旧分段扫描等待和新队列端到端模拟分别标注，不能称生产已提速。
