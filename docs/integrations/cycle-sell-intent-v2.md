# 循环可售意图投递契约 v2 增补（相对 v1）

> 状态：**已冻结的增补，尚未编码**。决策：D-0058。需求：00014。
> 规范性全文里，itsp 页面接口、出站、回调和商城差异都在 [坏件循环接口契约](cycle-repair-api.md)。本文件只列相对 [v1](cycle-sell-intent-v1.md) 的差异，给商城和 itsp 出站对照。两边冲突时以 `cycle-repair-api.md` 为准，并在同一次文档修改里改本文件。
> v1 正文保留，描述 main 上 #528/#529 的一件一行行为。D-0058 落地替换之后，新推送不再发送 v1 体。
> **本增补不打开 `SHOP_CYCLE_SELL_ENABLED`，不调用闲管家，不部署。**

## 1. 不改的部分

- 路径仍是商城 `POST /api/integrations/cycle/sell-intents`。itsp 的 `SHOP_CYCLE_SELL_URL` 仍必须等于这个路径，校验逻辑不用改路径。
- 鉴权仍是独立服务 token，`Authorization: Bearer`。不得复用目录 token、图库 token 或回调 token。
- 幂等键仍是 `deliveryId`。只有这个 `deliveryId` 已经把库存加上之后再到达，才不再加库存。仅仅插入过幂等行、库存还没加时，不得回 `unchanged`。
- 回执四态的含义不变：`applied` / `unchanged` / `blocked` / `conflict`。`unchanged` 只表示这个 `deliveryId` 先前已经把库存加上（含 `publish_disabled` 那种已加库存、发布未做的情况）。先前只是 `blocked` 且库存未加时，不得返回 `unchanged`。
- `blockCode` 稳定集合不变。
- itsp 领取、租约、回执 CAS、退避时间不变，见 v1 §5。本增补只改「哪些 HTTP 状态不再无限重试」和载荷字段。
- 四个发布开关的默认值不变，仍全部关闭。
- 禁止字段不变：项目、客户、SN、供应商、备注、成本、价格、人员、token。
- 回调 `POST /api/integrations/cycle/requeue` 的请求体不变，仍只有 `deliveryId`。

## 2. 请求差异

v1 新推送停止使用。v2 请求：

```json
{
  "schemaVersion": 2,
  "deliveryId": "550e8400-e29b-41d4-a716-446655440000",
  "recordId": "6f1c2c3e-7b8a-4d9e-8f10-223344556677",
  "partId": 101,
  "conditionBucket": "good",
  "qty": 12
}
```

| 字段 | v1 | v2 |
| --- | --- | --- |
| `schemaVersion` | 整数 `1` | 整数 `2` |
| 身份 | `unitId` 正整数，一件一行 | `recordId` UUID 字符串，一条维修结果。请求里不得再出现 `unitId` |
| `qty` | 恒为 `1` | 整数 1–1000，等于该结果的修好件数 |
| `conditionBucket` | `good` 或 `bad` | 本期 itsp 只发送 `good`。出现 `bad` 时商城按 v1 规则处理即可，但 itsp 本期不发送 |
| 其它 | 禁止字段 → 422 且不入库 | 同左。未知字段 → 422 且不入库 |

`qty` 是这一条推送要增加的好件库存，不是「再加 1」。废品不推送，不出现在请求里。

## 3. 回执差异

HTTP 200 体：

```json
{
  "schemaVersion": 2,
  "deliveryId": "550e8400-e29b-41d4-a716-446655440000",
  "recordId": "6f1c2c3e-7b8a-4d9e-8f10-223344556677",
  "partId": 101,
  "status": "applied",
  "blockCode": null
}
```

- 回执必须带回请求中的 `deliveryId`、`recordId`、`partId`，且 `schemaVersion` 为 2。对不上则 itsp 视为 `invalid_receipt`，不标 `delivered`。
- 回执不得再要求 `unitId`。带了 `unitId` 视为未知字段，itsp 按无效回执重试。
- `applied`：该 `deliveryId` 第一次把好件库存加上 `qty`。
- `unchanged`：该 `deliveryId` 先前已经把库存加上，本次不再加。itsp 仍只在持久 `receipt_status=applied` 时接受 `unchanged`。
- `blocked` / `conflict` 与 v1 相同。`publish_disabled` 仍表示库存已经加上、发布没有做；其它 blocked 码表示库存没加。

## 4. itsp 对 HTTP 状态的处理（相对 v1 §5 的显式变更）

v1 把所有非 200 都当成传输失败并按 5 秒起指数退避重试。v2 收窄如下，避免契约错误被重试放大：

| 商城响应 | itsp 意图状态 | 是否自动重试 |
| --- | --- | --- |
| 200 `applied` / 合法 `unchanged` | `delivered` | 停止 |
| 200 `blocked` 且 `blockCode` 在集合内 | `blocked` | 5 分钟 / 15 分钟 / 1 小时 / 6 小时，封顶 6 小时 |
| 200 `conflict` | `failed` | 不重试 |
| 409 | `failed`，`last_error_code=remote_conflict` | 不重试 |
| 422 | `failed`，`last_error_code=invalid_request` | 不重试 |
| 401 / 403 | `pending`，`last_error_code=credential_rejected` | 5 秒起指数退避，封顶 1 小时 |
| 404 / 408 / 429 / 5xx / 超时 / 无效回执 | `pending` | 同上 |
| 出站开关关闭或 URL 为空 | 保持 `pending` | worker 不发 HTTP |

401/403/404 会重试，是因为凭据或路由可以后来补上。422 与 409 重试也不会成功。

## 5. 商城必须实现的变更

itsp 本仓库读不到商城仓的当前 head。下列行为以本增补为准；表结构按商城现有 `cycle_sell_delivery` 适配，设计报告里的 `ALTER` 只是形状建议，不是已经核对过的行号。

1. 接收 `schemaVersion=2`。库存增加量是 `qty`，写 `stock_txn.qty = qty`。`external_reference`（或现有等价唯一键）仍是 `deliveryId`。只有该行已经 `stock_applied=true` 时，再次到达才不加库存。
2. 保存 `recordId` 与 `schemaVersion`。`unitId` 不再作为 v2 的必填身份。若表上仍有 `unit_id`，v2 行留空，不得再拿它做幂等。
3. 去掉「qty 只能为 1」的约束，改为整数 1–1000。
4. 同一 `deliveryId` 再次到达时，先比身份，再看库存有没有加上：
   - `partId`、`recordId`、`conditionBucket`、`qty` 任一与已存行不一致：返回 `conflict`，不改库存。
   - 身份一致且 `stock_applied=true`：返回 `unchanged`，不加库存。`publish_disabled` 属于这一类（库存已加、发布未做），重试只补发布。
   - 身份一致但库存还没加（例如上次是 `blocked/missing_list_price`）：重新检查档案、正式图、类目、标价。仍然缺少就返回当前的 `blocked`，不加库存。条件已经齐了就加上全部 `qty`，把 `stock_applied` 写成 true，返回 `applied`（发布开关关闭时改为 `blocked/publish_disabled`，库存只加这一次）。
5. 第一次见到这个 `deliveryId` 时，仍是一条商城事务：先占幂等行，再校验档案、正式图、类目、标价，条件齐了才 `qty_on_hand += qty`。缺条件时不加库存并返回对应 `blocked`，留下可重放的行。不得把这种行的下一次相同请求回成 `unchanged`。
6. 未知的 `schemaVersion=1` 且库里没有这个 `deliveryId`：返回 422，不建行。生产没有旧的循环投递；itsp 替换完成后也不再发 v1。已经存在的 v1 行用当时存下的 `unitId`、`partId`、`conditionBucket`、`qty` 比身份，不要求 `recordId`。一致且库存已加才 `unchanged`；一致但库存未加则重新判断；不一致则 `conflict`。
7. 合帖键仍是 `(partId, conditionBucket)`。本期只有好件数量会增加。不要为废品建帖，不要调用闲管家写接口。
8. 本期不要调用 itsp 的 `mark-sold` / `revert-sold` 来完成订单。这两个路径会返回 409 `phase_deferred` 并且不改 itsp。售出分配放到以后的独立账，不回写修好件数。

建议、不阻塞本阶段：库存流水用不同的 `kind` 区分「循环推送」和「Excel 入库」。修好的实物只走推送。本阶段不要求商城自动识别同一批实物被 Excel 再导一次。

## 6. 回调差异

| 路径 | v2 行为 |
| --- | --- |
| `POST /api/integrations/cycle/requeue` | 与 v1 §10.1 相同。只按 `deliveryId` 把 `blocked` 行的 `available_at` 调到现在 |
| `POST /api/integrations/cycle/mark-sold` | 凭据与 v1 相同。body 仍只接受 v1 形状（`schemaVersion=1`、`qty=1`）。合法请求返回 **409**，`detail` 为 `phase_deferred`，数据库零写入 |
| `POST /api/integrations/cycle/revert-sold` | 同上 |
| 回调 token 未配置 | 三个 POST 仍是 404 `not_enabled`，不解析 body |

`phase_deferred` 不是临时故障。商城不得把 409 再重试成成功，也不得改 itsp 库存或变卖台账。以后若启用售出回写，会换新的成功语义并另写一版增补；数量账本（待修 / 已上电商 / 废品）不因售出而改写。
