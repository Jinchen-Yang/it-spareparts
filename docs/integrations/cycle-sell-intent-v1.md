# 循环池可售意图投递契约 v1（itsp → spareparts-shop）

> 状态：**拟定契约**。生产者 itsp 已实现（T4 `SellIntentWorker`，默认关闭）；消费者 spareparts-shop **尚未实现**（T6 只读待核结论：无该路由、无闲管家客户端、无类目映射、无订单状态枚举）。
> 决策：D-0057（`docs/decisions/0037-坏件循环池与闲鱼可售意图.md`，PR #527）；需求：00014。
> 范围：跨仓 JSON 契约 + itsp 投递语义 + 商城侧拟定语义（T7 实现，需用户确认）。**本轮不打开 live、不部署、不合入。**

## 1. 开关与身份

| 开关 | 默认 | 作用 |
| --- | --- | --- |
| itsp `SHOP_CYCLE_SELL_ENABLED` | `false` | 是否发 HTTP；关闭时意图保持 `pending`，不伪造成功 |
| itsp `SHOP_CYCLE_SELL_URL` | 空 | 必须无凭据/查询串/fragment；路径必须是 `/api/integrations/cycle/sell-intents`；生产 HTTPS；开发仅回环 HTTP |
| itsp `SHOP_CYCLE_SELL_TOKEN` | 空 | 32–256 位无空白 ASCII `SecretStr`；不得等于目录/图库 token |
| shop 发布模式 | `off` | `off` / `dry_run` / `live` |
| shop `SHOP_CYCLE_PUBLISH_PART_ALLOWLIST` | 空 | live 时必须非空，只发布列出的 `partId` |
| shop `SHOP_CYCLE_PUBLISH_LIVE_CONFIRM` | `false` | live 还需为 true |

四个条件同时满足才对闲管家发写请求。**代码合并、测试通过、口头开工都不改变默认值。**
商城侧只存服务 token 的 SHA-256；不接受用户 JWT、目录 token、图库 token。

## 2. 请求

`POST /api/integrations/cycle/sell-intents`
`Authorization: Bearer <独立服务 token>`
`Content-Type: application/json`

```json
{
  "schemaVersion": 1,
  "deliveryId": "550e8400-e29b-41d4-a716-446655440000",
  "unitId": 42,
  "partId": 101,
  "conditionBucket": "good",
  "qty": 1
}
```

| 字段 | 约束 |
| --- | --- |
| `schemaVersion` | 整数 `1` |
| `deliveryId` | UUID 字符串；itsp 一单位一意图一投递 id |
| `unitId` | 正整数；itsp 循环单位 id |
| `partId` | 正整数；稳定 `part_id`，与单位一致 |
| `conditionBucket` | `good` / `bad` |
| `qty` | 恒为 `1`（itsp 一行一件） |

- 出现项目、客户、SN、供应商、备注、成本、价格、人员字段：HTTP 422，不入库；itsp 自身也不发送这些字段。
- 商城响应上限 4 KiB。

## 3. 回执

HTTP 200：

```json
{
  "schemaVersion": 1,
  "deliveryId": "550e8400-e29b-41d4-a716-446655440000",
  "partId": 101,
  "unitId": 42,
  "status": "applied",
  "blockCode": null
}
```

| status | 含义 | itsp 处理 |
| --- | --- | --- |
| `applied` | 该 `deliveryId` 第一次入账，且已按发布规则处理（或 dry_run 完成） | `delivered` |
| `unchanged` | **该 `deliveryId` 先前已经 `applied`**；库存不再加 | `delivered` |
| `blocked` | 条件不足，库存未加，帖子未新建 | `blocked`（按 §5 退避重试） |
| `conflict` | 身份冲突或人工保护，需人处理 | `failed`（不自动重试） |

- **`unchanged` 只表示「先前已 applied」**。先前只是 `blocked` 时，重放仍返回 `blocked`，或条件满足后返回 `applied`；禁止用 `unchanged` 表示"什么都没做"。
- itsp 对 `unchanged` 的接受条件：**持久证据显示该 delivery 上一次被商城接受的回执是 `applied`**。实现使用持久字段 `receipt_status`（不是展示用 `block_code`）；`block_code` 只是错误码展示，网络失败/无效回执只清展示码、不清 `receipt_status`。此前无 `applied` 证据（包括仅 blocked、network 失败、invalid_receipt）时收到 `unchanged`，itsp 视为 `invalid_receipt`（不标 delivered，保持 pending 退避并保留证据），直到收到 `applied`。
- `deliveryId`/`partId`/`unitId` 必须与领取行一致，否则 `invalid_receipt`，不标 `delivered`。
- 回执字段使用 pydantic `extra=forbid`（strict）；未知字段/类型错误 = 无效回执。

### 3.1 blockCode 稳定集合

`missing_formal_image`、`missing_category_mapping`、`missing_list_price`、`missing_catalog_identity`、`publish_api_missing_stock_update`、`publish_disabled`。

- `publish_disabled`：发布开关关闭（off / live 条件不足）。测试模式可以入账但不上架；生产 live 未开启时返回该码，itsp 保持 `blocked`，不标 delivered。
- **T6 实测：商城当前无闲管家客户端（创建/改库存/下架/重上架 0/4）**，因此 live 路径一律返回 `publish_api_missing_stock_update`（或该码替代），不创建第二帖、不调外网写。
- 商城缺类目映射（当前无映射表）→ `missing_category_mapping`；缺正式图/标价同理。
- `blocked` 必须携带上述集合内的 `blockCode`；否则 itsp 按无效回执重试（不进入 blocked 退避）。

### 3.2 补条件续跑（requeue）契约（本轮 itsp **未实现**；文本保留供 T7 对齐）

- 商城在正式图/类目映射/标价从缺变齐后，尽力调用 itsp：`POST /api/integrations/cycle/requeue`，body **只有** `{"deliveryId": "<uuid>"}`。
- 独立服务凭据 `CYCLE_SELL_CALLBACK_TOKEN`（与出站 `SHOP_CYCLE_SELL_TOKEN` 不同）：**发送方商城**在秘密存储中持有调用所需原始 token；**接收方 itsp**仅保存/配置该 token 的 SHA-256 摘要，并对传入 `Authorization: Bearer` 的 token 计算 SHA-256 比较（`SecretStr` 只包装摘要）。
- itsp 行为：仅当该 `deliveryId` 行当前 `state='blocked'` 时，把 `available_at = now()`；其他状态幂等返回当前状态、不改行为。
- webhook 丢失只影响速度，不丢正确性；itsp 自身 blocked 重试（5m/15m/1h/6h）是第二路。
- **实现状态（T1–T5）**：该端点与回调凭据**未实现（范围调整）**；轮询重试已保证最终重试；T7 商城接入时另案实现并独立安全审查。

## 4. 商城侧语义（拟定；T7 实现）

- 幂等账 `cycle_sell_delivery(delivery_id PK, part_id, condition_bucket, stock_applied, publish_applied, applied_at)`；`cycle_listing` 唯一 `(part_id, condition_bucket)`；`cycle_allocation` 唯一 `(shop_order_line_id, delivery_id)`。
- 处理顺序（同一商城事务）：
  1. 插入幂等账；主键冲突且旧行已 applied → 回 `unchanged`，不改数量。
  2. 校验 active part/catalog identity → 缺则 `blocked/missing_catalog_identity`。
  3. 校验正式图、类目映射、标价 → 缺则 `blocked` 对应码；不增加 `qty_on_hand`。
  4. 条件齐备：`qty_on_hand += 1`，再谈发布。
  5. 发布模式 `off`：提交库存，回 `blocked/publish_disabled`；重试只补发布、不重复加库存（`stock_applied`/`publish_applied` 分开记录）。
  6. `dry_run`：写本地帖，`external_listing_id` 空；回 `applied`。
  7. `live`：调闲管家；当前无客户端 → 保持 `publish_api_missing_stock_update`，不创建第二帖。
- 禁止为补货删除旧帖再发新帖；数量降 0 时下架/库存设 0 并保留商品 id（`sold_out`），再次入账用原 id 重新上架。

### 4.2 售出分配与超卖（拟定；T9 实现，本轮不编码）

- 可分配集合：该合帖下 `stock_applied=true` 且尚未分配（无未作废 `cycle_allocation`）的 delivery，按 `(applied_at, delivery_id)` 升序取。
- 订单件数大于可分配件数：**整笔分配回滚**，订单进入商城异常队列；不得把差数记成负库存，不得部分标记 itsp 单位 sold。
- 每件分配成功后调用 `POST /api/integrations/cycle/mark-sold`（body `{schemaVersion, deliveryId, shopOrderLineId, qty:1}`）：
  - `deliveryId` 对应单位 outcome 已是 `sold` 且事件中同一 `shopOrderLineId` → 200 当前状态，不第二次写；
  - outcome=`to_shop` → 改 `sold` 并写事件；其他 outcome → 409，商城把分配标 `remote_conflict`，不重试成另一件。
- 人工主管 mark-sold（T2 已实现）与商城回调冲突：人工路径 `expected_version` CAS，回调按 delivery 幂等；冲突 409，不覆盖。
- 未发货取消/退款：作废 allocation、数量加回、按 §8.4 补货，调用 `revert-sold`；仅当前 sold 且同一 `shopOrderLineId` 才回 `to_shop`；已发货退货不改 itsp。
- 商城分配账、超卖和订单枚举仍未实现。itsp 收到回调后的状态码见 §10。
## 5. itsp 投递语义（T4 实现）

- 领取：`FOR UPDATE SKIP LOCKED`；`pending` 到期、`delivering` 租约过期、`blocked` 到期；租约 60s；语句超时 5s、锁超时 1s；事务在 HTTP 前结束。
- HTTP：总期限 15s、不跟随重定向、`trust_env=false`、响应 ≤4 KiB、`content-encoding` 必须 identity。
- 回执落库：租约 CAS（`state=delivering` + `lease_token` + `lease_until>now`）；过期工人回执影响 0 行。
- 日志只有 `deliveryId`、`partId`、结果码、是否接受；无 traceback/token/body。
- 错误码与退避：

| 结果 | 意图态 | 下次 |
| --- | --- | --- |
| `applied` / `unchanged`（**仅当持久 `receipt_status=applied`**） | `delivered` | 停止 |
| `blocked`（blockCode 在集合内） | `blocked` | 5 分钟 / 15 分钟 / 1 小时 / 6 小时（封顶 6h） |
| `conflict` | `failed` | 不自动重试 |
| 超时 / 传输失败 / 非 200 / 无效回执 | `pending` | 5s 起指数退避，封顶 1h |

- 网络失败不写 `blocked`；`blocked` 只表示商城明确说业务条件不足。
- 默认关闭；未启用/URL 空时 worker 不启动网络循环，意图保持 `pending`。
- worker 轮询间隔 5s（无 after-commit 唤醒，只影响延迟、不影响正确性）。

## 6. 售出与取消（本轮范围）

- **mark-sold 人工主管兜底归 T2**：`closed` + `to_shop` → `sold`，`expected_version`；**幂等仅限当前版本**（旧 expected_version → 409），不能以幂等代替 CAS；若存在 pending/blocked 可售意图则同事务置 `failed/unit_sold_manually` 阻止 worker 投递，`delivering` 时 409。人工路径不读 `shopOrderLineId`。
- itsp 入站回调的路径、字段和状态码以 §10 为准。商城侧的分配账、超卖和订单枚举仍未实现；已发货退货不改 itsp。

## 7. 禁止字段与安全

- 请求/回执禁止：项目、客户、SN、供应商、备注、成本、价格、人员显示名、token。
- token 只进环境变量/秘密存储；不进 git/PR/日志/前端包/浏览器。
- 日志禁止项目、SN、供应商、价格、token、HTTP body。

## 8. 未实现声明与边界

- **商城侧尚未实现本契约**：无 `/api/integrations/cycle/sell-intents`、无 cycle 表、无闲管家客户端、无类目映射、无订单/支付/发货/取消/退款枚举。本文档为拟定契约；T7+ 需用户确认后才编码。
- itsp 侧 T4 worker 默认关闭；本轮未连真实商城/闲管家；未打开 live；未部署。
- 真实上架、闲管家 API、合帖仍未实现。itsp 入站见 §10，默认不连真实商城。

## 附录 A：T6 商城只读待核结论（`origin/main = 7723be7`）

| # | 待核项 | 结论 |
| --- | --- | --- |
| 1 | 目录接收端点 | 存在 `POST /api/integrations/catalog/parts/sync`；回执枚举仅 `applied/unchanged/conflict`（无 `blocked`）→ cycle 必须独立路由/表 |
| 2 | D-36 好坏零库存 SKU | `sku` 表，`grade ∈ {good,bad}`，`UNIQUE(pn_id,grade)`，`qty_on_hand` 默认 0 |
| 3 | 正式图判定 | 无单一函数；口径 = `image.status='gallery'` ∧ `is_defect=false` ∧ 未删除；主图按 `(sort_order,created_at,id)` 第一张 |
| 4 | 闲鱼/闲管家类目映射 | **仓库中不存在**（`category_major/minor` 是内部品类，非平台类目） |
| 5 | 闲管家客户端 | **不存在**；创建/改库存/下架/重上架 **0/4** → `publish_api_missing_stock_update`；T8 live 外网写不可做 |
| 6 | 订单/支付/发货/取消/退款枚举 | **仓库中不存在**；T9 revert-sold 暂不实现，取消先入商城异常队列 |
| 7 | 幂等/token 摘要范式 | `catalog_delivery_receipt`、`integration_request_log`、env 存 SHA-256（可仿） |
| 8 | `sell-intents` 路由是否已存在 | **不存在**（0 命中）→ 可安全新增，迁移 `0028+` |
| 9 | 商城需求台账下一号 | `00002`；决策入口 `docs/09 §0` |
| 10 | 测试数据库起法 | 本机 PG15 `TEST_DATABASE_URL`、compose `db` 服务、CI helper；均无需删除已有容器 |

报告：`team/reports/tech/2026-11-08-T6商城待核.md`（十项均带仓库内路径/命令证据）。

## 9. 实现状态与范围调整（相对任务书 §8；T5 交付时如实登记）

> 本节记录契约文本和 itsp 代码的差异。商城合帖、闲管家和订单账不在 itsp。

| 任务书条目 | 本轮状态 | 说明 |
| --- | --- | --- |
| §8.1 请求格式/禁止字段 | **已实现（itsp）** | T4 `SellIntentWorker` 只发送 schemaVersion/deliveryId/unitId/partId/conditionBucket/qty；禁止字段不出现在载荷/日志 |
| §8.2 回执四态与 `unchanged` 语义 | **已实现（itsp）** | applied→delivered；blocked→blocked；conflict→failed；**`unchanged` 仅在持久 `receipt_status=applied` 时接受**（无 applied 证据时视为 invalid_receipt 保持 pending，R9）；网络/无效回执不清持久证据 |
| §8.3 blocked 双路续跑（itsp 重试 + 商城 requeue 回调） | **itsp 已实现；商城未实现** | itsp 阻塞重试与 `POST /api/integrations/cycle/requeue` 见 §10。商城何时调用仍未实现；丢失 webhook 只影响速度 |
| §8.3 商城补条件后主动回调 | **未实现（商城侧）** | 商城尚未实现；itsp 接收端按 §10 待命 |
| §8.4 合帖/上下架/售出/补货 + dry_run test_mode 隔离 | **未实现（商城侧，T7 拟定）** | itsp 不实现商城发布逻辑，不调用闲管家。dry_run 语义仍只存在于契约 |
| §8.5 售出分配/超卖/取消/人工冲突 | **itsp 入站已实现；商城分配账未实现** | 人工 mark-sold 保持 T2。服务凭据 mark-sold / revert-sold 见 §10。商城分配、超卖和对账不在 itsp |
| §8.6 开关默认值 | **itsp 已实现；shop 侧未实现** | 出站开关默认关。回调摘要未配置时入站 404。shop 发布模式 `off`、allowlist、live confirm 仍只在契约里 |
| 回调凭据分离（出站 vs 回调） | **itsp 已实现** | 商城持有原始 `CYCLE_SELL_CALLBACK_TOKEN`；itsp 只配置 `CYCLE_SELL_CALLBACK_TOKEN_SHA256`。摘要不得等于出站 token 的 SHA-256 |
| 主管投递状态查询 API | **已接线** | `GET /api/cycle/units/{id}/sell-intent`，仅 `page_cycle_manage`；响应等于 `read_status()` |

商城合帖、闲管家客户端、类目映射、订单枚举、真实上架和 live 开关仍然没有实现。itsp 入站打开后默认不发任何出站 HTTP。

## 10. itsp 入站与主管查询（协议冻结）

itsp 不新增表、不新增列。幂等靠 `maintenance_cycle_event.payload.shopOrderLineId`。回调凭据是环境变量里的 SHA-256，不入库。不写 `inventory`、`stock_dynamic`、`maintenance_bad_salvage`。日志只有 `deliveryId` 和结果码。

配置：`CYCLE_SELL_CALLBACK_TOKEN_SHA256`，64 位小写十六进制。空则下列三个 POST 一律 **404** `not_enabled`，不解析 body。已配置时校验 `Authorization: Bearer`，原始 token 须为 32–256 位无空白 ASCII，摘要用 `hmac.compare_digest`。失败 **401** `credential_invalid`。该摘要不得等于 `SHOP_CYCLE_SELL_TOKEN` 的 SHA-256。

未知 JSON 字段 **422**。`Cache-Control: no-store`。

### 10.1 `POST /api/integrations/cycle/requeue`

Body 只有 `{"deliveryId":"<uuid>"}`。

| 条件 | HTTP | body |
| --- | --- | --- |
| 无此 `deliveryId` | 404 | `{"detail":"not_found"}` |
| `state=blocked` | 200 | 把 `available_at=now()`，`state` 与 `blockCode` 不变 |
| 其他状态 | 200 | 不改行 |

200 body：`schemaVersion=1`，`deliveryId`，`status`（当前 state），`requeued`（仅本次从 blocked 提前时为 true），`nextAttemptAt`（pending/blocked 为 `available_at` 的 ISO-8601，否则 null）。

### 10.2 `POST /api/integrations/cycle/mark-sold`

```json
{"schemaVersion":1,"deliveryId":"<uuid>","shopOrderLineId":"line-1","qty":1}
```

`shopOrderLineId` 匹配 `^[A-Za-z0-9._:-]{1,64}$`。`qty` 只能是 1。

先读出 `unit_id`，再锁单位，再锁意图。锁序与人工 mark-sold 相同，避免交叉等待。

| 条件 | HTTP |
| --- | --- |
| 无此意图 | 404 `not_found` |
| outcome 已是 `sold`，且最新一条 `mark_sold` 事件的 `shopOrderLineId` 相同 | 200，不写第二笔 |
| `station=closed` 且 outcome 为 `to_shop`，意图不是 `delivering`/`failed` | 200，outcome 改为 `sold`，追加事件 |
| 其余 | 409 `remote_conflict`，不覆盖 |

`pending`/`blocked` 同事务改为 `failed`，`last_error_code=unit_sold_by_callback`。`delivered` 保持不动。事件 actor 为 `integration:cycle-sell`，payload 只有 `outcome`、`shopOrderLineId`、`qty`。

200 body：`schemaVersion`，`deliveryId`，`unitId`，`outcome`，`shopOrderLineId`，`idempotent`。

### 10.3 `POST /api/integrations/cycle/revert-sold`

Body 与 mark-sold 相同。只把单位 outcome 从 `sold` 改回 `to_shop`，工位保持 `closed`，不新建意图，不改商城库存。

| 条件 | HTTP |
| --- | --- |
| 无此意图 | 404 `not_found` |
| outcome 为 `to_shop`，且晚于最近一次 `mark_sold` 的 `revert_sold` 使用同一 `shopOrderLineId` | 200，不写第二笔 |
| outcome 为 `sold`，且最新 `mark_sold` 的 `shopOrderLineId` 相同 | 200，改回 `to_shop` 并追加 `revert_sold` |
| 其余，含已发货退货被误调用 | 409 `remote_conflict` |

### 10.4 `GET /api/cycle/units/{id}/sell-intent`

用户 JWT。仅 `page_cycle_manage`。单位不可见为 404。响应等于 `read_status()`：无意图时 `{"unit_id","status":"not_queued"}`；有意图时含 `deliveryId`、`status`、`attempts`、`blockCode`、`lastErrorCode`、`receiptStatus`、`nextAttemptAt`、`createdAt`、`updatedAt`、`completedAt`。
