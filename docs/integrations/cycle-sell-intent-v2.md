# 循环可售意图投递契约 v2 增补（相对 v1）

> 状态：**已冻结的增补，尚未编码**。决策：D-0058。需求：00014。
> 对照：商城 PR #91。线路以商城为准。金样例是该 PR head `fdb29abc` 的 `docs/integrations/cycle-sell-intent-v2-examples.json` 原样拷贝，不改键、不改用例。
> 金样例：[cycle-sell-intent-v2-examples.json](cycle-sell-intent-v2-examples.json)。八个用例：`applied`、`unchanged`、`blocked_missing_catalog_identity`、`blocked_missing_operator`、`validation_422`、`merge_409_retryable`、`applied_v1`、`conflict_v2_bad`。每个 HTTP 200 的 `responseBody` 恰好是 `schemaVersion`、`deliveryId`、`unitId`、`partId`、`status`、`blockCode`。
> 页面接口仍以 [cycle-repair-api.md](cycle-repair-api.md) 为准。本文件与该契约冲突时，线路字段以本文件和金样例为准，并在同一次修改里改两边。
> **本增补不打开 `SHOP_CYCLE_SELL_ENABLED`，不调用闲管家，不部署。**

## 1. 不改的部分

- 路径仍是商城 `POST /api/integrations/cycle/sell-intents`。itsp 的 `SHOP_CYCLE_SELL_URL` 仍必须等于这个路径。
- 鉴权仍是独立服务 token，`Authorization: Bearer`。不得复用目录 token、图库 token 或回调 token。
- 幂等键仍是 `deliveryId`。
- 回执四态的名字不变：`applied` / `unchanged` / `blocked` / `conflict`。
- itsp 领取、租约、回执 CAS、退避时间见 v1 §5。本增补改载荷身份、`blockCode` 集合，以及 409 的处理。
- 四个发布开关的默认值不变，仍全部关闭。
- 禁止字段不变：项目、客户、SN、供应商、备注、成本、价格、人员、token，以及维修结果的 UUID。
- 回调 `POST /api/integrations/cycle/requeue` 的请求体不变，仍只有 `deliveryId`。本期商城不调用 requeue、mark-sold、revert-sold。一次推送的成败只看这一次 POST 的 HTTP 回执，没有 webhook。

`blockCode` 接受集合是 v1 的六个码，再加 `missing_operator`：

`missing_formal_image`、`missing_category_mapping`、`missing_list_price`、`missing_catalog_identity`、`publish_api_missing_stock_update`、`publish_disabled`、`missing_operator`。

商城 v2 本期只返回 `missing_catalog_identity` 和 `missing_operator`。其余码仍接受，避免旧回执被当成无效。集合外的码仍是 `invalid_receipt`。

## 2. 请求

商城保持现有六键，`extra=forbid`。itsp 不得增加键，不得发送 `recordId`。

下面与金样例 `applied` 的请求相同。完整八例以 JSON 文件为准。

```json
{
  "schemaVersion": 2,
  "deliveryId": "550e8400-e29b-41d4-a716-446655440000",
  "unitId": 42,
  "partId": 101,
  "conditionBucket": "good",
  "qty": 5
}
```

| 字段 | 约束 |
| --- | --- |
| `schemaVersion` | 整数 `2` |
| `deliveryId` | UUID 字符串。一条意图一个，重发不变 |
| `unitId` | 正整数，1 至 2^63−1。等于该维修结果的 `wire_unit_id`：插入时取自 `maintenance_repair_record_no_seq`，作废不回收，同一 `deliveryId` 永远发同一个数。`record_id`（UUID）只留在 itsp 内部 |
| `partId` | 正整数，稳定 `part_id` |
| `conditionBucket` | 本期只发送 `good`。itsp 不发送 `bad`，废品不生成意图。若商城收到 v2 的 `bad`，回 `conflict` 且不入库 |
| `qty` | 整数 1–1000，等于修好件数 |

未知字段（包括 `recordId`）→ HTTP 422，不入库。itsp 把 422 标为 `failed`，不重试。

商城继续接受 v1：`schemaVersion=1` 且 `qty=1` 的六键请求仍按件入库。itsp 替换完成后不再发送 v1。不要把「商城拒绝新的 v1」写成契约。

## 3. 回执

每个 HTTP 200 的回执恰好六个键，顺序与金样例一致：`schemaVersion`、`deliveryId`、`unitId`、`partId`、`status`、`blockCode`。回显 `unitId`，不加 `qty`，不出现 `recordId`。`applied_v1` 的 `schemaVersion` 为 1，键集合相同。

```json
{
  "schemaVersion": 2,
  "deliveryId": "550e8400-e29b-41d4-a716-446655440000",
  "unitId": 42,
  "partId": 101,
  "status": "applied",
  "blockCode": null
}
```

itsp 用 `extra=forbid` 解析。缺 `unitId`、多 `recordId`、或其它未知键 → `invalid_receipt`，不标 `delivered`。

回显身份必须与发出的一致：`schemaVersion=2`，`deliveryId` 相同，`unitId` 相同，`partId` 相同。不一致 → `invalid_receipt`。

| status | itsp 处理 |
| --- | --- |
| `applied` | `delivered`。持久 `receipt_status=applied` |
| `unchanged` | 回显身份一致即 `delivered`，并把持久 `receipt_status` 写成 `applied`。不再要求 itsp 先前见过 `applied`。商城保存经办人时会把尚未入库的 `missing_operator` 行在同一次事务里入账，下一次 POST 直接是 `unchanged` |
| `blocked` | `blockCode` 在第 1 节集合内才进入 `blocked`，否则 `invalid_receipt` |
| `conflict` | `failed`，不自动重试。v2 的 `bad` 走这条 |

`missing_operator` 是延迟入库：商城还没加库存，等经办人保存后才会加。它不是「确定不会入库」的 blocked。`missing_catalog_identity` 才是未加库存、目录补上后同一 `deliveryId` 可以变成 `applied`。

`publish_disabled` 仍按 v1 理解为库存已加、发布未做。商城 v2 不因缺图、缺类目、缺标价或发布开关返回这个码。

## 4. itsp 对 HTTP 状态的处理

| 商城响应 | itsp 意图状态 | 是否自动重试 |
| --- | --- | --- |
| 200 `applied` / 身份一致的 `unchanged` | `delivered` | 停止 |
| 200 `blocked` 且 `blockCode` 在集合内 | `blocked` | 5 分钟 / 15 分钟 / 1 小时 / 6 小时，封顶 6 小时。`missing_operator` 同样走这档 |
| 200 `conflict` | `failed` | 不重试 |
| 422 | `failed`，`last_error_code=invalid_request` | 不重试 |
| 409 | `pending`，`last_error_code=remote_busy` | 5 秒起指数退避，封顶 1 小时。商品合并进行中、库存超上限等都算暂时失败，合并结束后同一 `deliveryId` 仍可入账 |
| 401 / 403 | `pending`，`last_error_code=credential_rejected` | 同上，5 秒起，封顶 1 小时 |
| 404 / 408 / 429 / 5xx / 超时 / 无效回执 | `pending` | 同上 |
| 出站开关关闭或 URL 为空 | 保持 `pending` | worker 不发 HTTP |

401/403/404/409/5xx 会重试。422 重试也不会成功。

## 5. 商城侧行为（以商城 v2 为准，itsp 不另加门槛）

1. 接收第 2 节的六键。`unitId` 必填，对应商城现有 `unit_id`（保持非空）。不接收维修结果 UUID。
2. `qty` 为整数 1–1000。库存增加量是 `qty`。`external_reference`（或现有等价唯一键）仍是 `deliveryId`。
3. 身份是 `schemaVersion` + `unitId` + `partId` + `conditionBucket` + `qty`。同一 `deliveryId` 再次到达时身份不一致 → `conflict`，不改库存。
4. 入库条件只有两条：目录身份、经办人。不检查正式图、类目映射、标价，也不因发布开关返回 `publish_disabled`。
   - 没有目录身份 → `blocked/missing_catalog_identity`，不加库存。目录补上后，同一 `deliveryId` 重放可以 `applied`。
   - 没有经办人 → `blocked/missing_operator`，先不加库存。`set_operator` 在同一事务里把这些行入账，不等 itsp 再 POST。itsp 下一次 POST 得到身份一致的 `unchanged`。
   - 两条都齐 → 加上全部 `qty`，回 `applied`。
5. `unchanged` 只在该 `deliveryId` 的库存已经加上之后返回（商城不变量：`unchanged` ⇔ `stock_applied`）。itsp 侧仍按第 3 节接受，不要求自己事先存过 `applied`。
6. v2 且 `conditionBucket=bad` → `conflict`，不入库、不建帖。
7. v1 `schemaVersion=1`、`qty=1` 继续接受。itsp 新代码不发 v1。
8. 本期不调用 itsp 的 requeue、mark-sold、revert-sold。itsp 上 mark-sold / revert-sold 仍对合法 v1 体返回 409 `phase_deferred` 且零写入。

打开 itsp 出站开关之前，目标环境必须已经部署商城 v2，并且商城经办人已经配置好。否则推送会停在 `missing_operator`，作废之后商城仍可能在补经办人时把库存加上。

建议、不阻塞本阶段：库存流水用不同的 `kind` 区分「循环推送」和「Excel 入库」。修好的实物只走推送。本期不要求商城自动拦住 Excel 再导同一批货。

## 6. 回调差异

| 路径 | v2 行为 |
| --- | --- |
| `POST /api/integrations/cycle/requeue` | 与 v1 §10.1 相同。只按 `deliveryId` 把 `blocked` 行的 `available_at` 调到现在。本期商城不调用 |
| `POST /api/integrations/cycle/mark-sold` | 凭据与 v1 相同。body 仍只接受 v1 形状（`schemaVersion=1`、`qty=1`）。合法请求返回 **409**，`detail` 为 `phase_deferred`，数据库零写入。这是 itsp 入站的延期，不是商城出站的 HTTP 409 |
| `POST /api/integrations/cycle/revert-sold` | 同上 |
| 回调 token 未配置 | 三个 POST 仍是 404 `not_enabled`，不解析 body |

`phase_deferred` 不是暂时故障。商城本期不调用这两条。数量账本（待修 / 已上电商 / 废品）不因售出而改写。
