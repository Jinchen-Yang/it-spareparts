# 坏件循环接口契约（D-0058）

> 状态：**已冻结，尚未编码**。决策：[D-0058](../decisions/0038-坏件循环待修池与结果记录.md)。
> 范围：itsp 页面接口、itsp → 商城出站、商城 → itsp 回调。出站相对 v1 的差异摘要在 [cycle-sell-intent-v2.md](cycle-sell-intent-v2.md)。冲突时以本文件为准。
> 前缀：itsp 用户接口与回调都挂在 `settings.api_prefix`（`/api`）。商城接收路径是商城自己的 `/api/integrations/cycle/sell-intents`。
> 本文件不授权打开 `SHOP_CYCLE_SELL_ENABLED`，不授权真实闲鱼发帖，不授权部署。

内部 JSON 用 snake_case。跨仓 JSON 用 camelCase。用户接口的错误体沿用现有循环接口：`{"detail": "<中文句子>"}`，`detail` 必须是字符串，因为页面用 `detailOf` 只展示字符串。下表的 code 是实现和测试用的稳定名字，不放进 `detail`。

返还收货自己的接口保持原有错误形状。重新导入用 `ImportError(code, message, 422)`。

## 1. 身份与页键

写操作和读操作都走现有 `cycle_actor`：必须是在职实名 `sys_user`。共享口令、旧 token、未登录 → 403，`detail` 为「坏件循环操作需要实名系统账号」。

| 页键 | 本阶段能力 |
| --- | --- |
| `page_cycle_repair` | 待修池、提交结果、自己的记录、当天作废自己的记录 |
| `page_cycle_wip` | 采购三数，只读 |
| `page_cycle_manage` | 全部读、作废任何记录、推送状态。包含维修员和采购的读能力 |
| `page_cycle_recycle`、`page_cycle_test` | 无能力。持有这两个键也不放行，403「无权访问坏件循环页面」 |

页键默认全关，含 admin。本阶段不新增页键，也不在迁移里把任何键写成 true。

主管响应可以多字段。维修员响应只能是下文字段，多出来的字段视为契约失败。采购响应同理。

## 2. 替换掉的现有接口

下列路径在替换 PR 中删除，不保留 410 影子，避免两套真值。

| 现路径 | 现用途 | 替代 |
| --- | --- | --- |
| `POST /api/cycle/units/handoff-from-receipt` | 从返还行逐件入池 | 删除。坏返自动出现在待修池 |
| `POST /api/cycle/units/intake-external` | 不占收货数量的外部入池 | 删除。本期不接外部送修 |
| `POST /api/cycle/units/{id}/dispatch` | 派工 | 删除 |
| `POST /api/cycle/units/{id}/self-pass` | 自测通过 | 删除 |
| `POST /api/cycle/units/{id}/complete-test` | 检测 | 删除 |
| `POST /api/cycle/units/{id}/complete-repair` | 逐件维修 | `POST /api/cycle/repair-records` |
| `POST /api/cycle/units/{id}/outsource-return` | 委外回来 | 删除 |
| `POST /api/cycle/units/{id}/close` | 结案，含回公司 | 删除。修好由提交结果直接推送 |
| `POST /api/cycle/units/{id}/withdraw` | 撤回仍在池内的件 | 删除。作废结果才退回待修 |
| `POST /api/cycle/units/{id}/claim` | 领取待办 | 删除，并注销 `cycle_test` / `cycle_repair` subject |
| `POST /api/cycle/units/{id}/mark-sold` | 主管人工售出 | 删除。本期没有售出状态 |
| `GET /api/cycle/units` | 按工位列表 | `GET /api/cycle/pool` 与 `GET /api/cycle/repair-records` |
| `GET /api/cycle/units/{id}` | 单位详情 | `GET /api/cycle/repair-records/{record_id}` |
| `GET /api/cycle/units/{id}/events` | 工位事件 | 删除。主管看扣数来源 |
| `GET /api/cycle/wip` | 四工位在制 | `GET /api/cycle/summary` |
| `GET /api/cycle/units/{id}/sell-intent` | 主管看投递 | `GET /api/cycle/repair-records/{record_id}/sell-intent` |

回调三个路径保留，行为见第 6 节。

## 3. 公共查询与幂等

### 3.1 分页

`page` 从 1 开始，默认 1。`page_size` 默认 20，最大 100。响应：

```json
{"items": [], "total": 0, "page": 1, "page_size": 20}
```

排序写在各接口下。超过范围的 `page` 返回空 `items`，`total` 仍是全量。

### 3.2 搜索

`q` 可选，最长 128。匹配当前 `dim_part.pn_std` 或 `dim_part.description` 的包含关系，转义 LIKE 通配符。不搜索项目、客户、供应商、返还单号。

### 3.3 写操作幂等

`POST /api/cycle/repair-records` 与作废都要求请求头 `Idempotency-Key`：1–128 位、无空白的可见 ASCII。

落库表 `maintenance_repair_command`，主键是 `(actor, idempotency_key)`，不要只用键做全局主键，避免一个账号读到另一个账号的响应。另存 `action`（`submit` / `void`）和 `record_id`（作废时为路径里的结果 id，提交时为空）。

指纹算法沿用现场问题命令：对下面这个对象做 `json.dumps(..., sort_keys=True, separators=(",", ":"), ensure_ascii=False)` 再取 SHA-256。指纹覆盖动作、目标记录和 body，不含 `Idempotency-Key` 头。这样同一 body 打到两条不同的作废路径不会被当成同一次请求。

```json
{"action": "void", "record_id": "6f1c2c3e-7b8a-4d9e-8f10-223344556677", "body": {"expected_version": 1, "reason": "填反了", "shop_stock_manually_reduced": false}}
```

提交时 `record_id` 为 `null`，目标型号和数量在 `body` 里。重放前先比对已存的 `action` 与 `record_id`，再比对指纹。任一不一致都拒绝，不返回上一次的响应。

同一事务里先 `pg_advisory_xact_lock(hashtextextended(actor || ':' || key, 0))`，再读命令行。这与 `maintenance_project_operations._lock_idempotency_key` 同一手法。

| 情况 | 结果 |
| --- | --- |
| 无此键 | 执行业务，成功后写入命令行（含动作、目标记录、HTTP 状态与响应体） |
| 同一账号、同一键、同一动作、同一 `record_id`、同一指纹 | 原样返回第一次的状态和响应体，不再写业务表。响应头 `Idempotent-Replayed: true` |
| 同一键但动作、`record_id` 或指纹不同，或键属于别的账号 | 409，`detail` 为「幂等键已被用于不同的请求」 |
| 缺少头或格式不对 | 400，`detail` 为「Idempotency-Key 须为 1–128 位无空白 ASCII」 |

业务失败（4xx）不写命令行，调用方用同一键修正后可以重试。只有成功响应被记住。命令行不是数量账本，删除它只影响重放，不改变待修数。

## 4. itsp 用户接口

### 4.1 `GET /api/cycle/pool`

需要 `page_cycle_repair` 或 `page_cycle_manage`。只有采购键 → 403。

查询：`q`、`page`、`page_size`。只返回 `pending_qty > 0` 且档案 `status=active` 的 PN。排序：`pn` 升序，`part_id` 升序。

维修员每行只有：

```json
{"part_id": 101, "pn": "MZ7LH960HAJR-00005", "description": "固态硬盘", "pending_qty": 12}
```

主管每行相同，本接口不加来源。描述来自 `dim_part.description`，空则为 `null`。

### 4.2 `POST /api/cycle/repair-records`

维修员或主管。201。

```json
{"part_id": 101, "fixed_qty": 2, "scrap_qty": 1}
```

`extra=forbid`。`part_id` 为正整数。两个数量为 0–1000 的整数。不得传 PN 字符串、SN、价格、来源或人员字段。

处理（一个数据库事务）：

1. 锁幂等键。
2. 共享 `DATA_CHANGE` 顾问锁（现有 `lock_receipt_context`）。
3. 对该 `part_id` 取维修顾问锁（见实施计划的锁顺序）。
4. 按 `rkd_line_id` 锁定该 PN 的返还行，再按先进先出分配。
5. 合计必须 ≥ 1 且 ≤ 当前待修。档案必须是 `active`。
6. 写入结果、扣数来源。`fixed_qty > 0` 时同一事务插入一条 `pending` 可售意图，`qty=fixed_qty`，`condition_bucket=good`。`fixed_qty=0` 不插意图。
7. 提交后才允许 worker 发 HTTP。本接口不等待商城。

成功体（维修员与主管相同，不含来源和 `delivery_id`）：

```json
{
  "record_id": "6f1c2c3e-7b8a-4d9e-8f10-223344556677",
  "record_no": "WX00000042",
  "part_id": 101,
  "pn": "MZ7LH960HAJR-00005",
  "description": "固态硬盘",
  "fixed_qty": 2,
  "scrap_qty": 1,
  "status": "active",
  "version": 1,
  "created_at": "2026-11-11T08:00:00+00:00",
  "operator": "zhang"
}
```

`operator` 是账号名，供「我的记录」核对。维修员列表也返回它，但服务端强制只查本人，不能靠这个字段看别人。

| 条件 | HTTP | detail |
| --- | --- | --- |
| 两数都是 0，或不是整数 | 422 | 由请求模型产生 |
| 合计超过当前待修 | 409 | 修好数与废品数之和超过当前待修件数 |
| 该 PN 待修为 0，或不是 active 档案 | 400 | 该型号当前没有待修件数 |
| 行锁超时 `55P03` / 死锁 `40P01` / 序列化失败 `40001` | 409 | 该型号正在被其他操作处理，请稍后重试 |

并发下第二次精确请求不得被改成「只扣剩余」。客户端要 3 件而只剩 2 件时返回 409，不改成 2。

### 4.3 `GET /api/cycle/repair-records`

需要 `page_cycle_repair` 或 `page_cycle_manage`。只有采购键 → 403。有维修员键时只看 `operator` 等于自己的行，忽略任何试图放宽的查询。主管可看全部。

查询：`q`、`status`（`active` / `voided`，缺省两者）、`part_id`、`page`、`page_size`。排序：`created_at` 降序，`record_id` 降序。

维修员行字段与 4.2 成功体相同，另加 `voided_at`、`void_reason`（未作废则为 null）。不含扣数来源、项目、`delivery_id`。

### 4.4 `GET /api/cycle/repair-records/{record_id}`

维修员：不是自己的记录 → 404，`detail` 为「记录不存在」。主管：200，在 4.3 字段之外增加：

```json
{
  "sources": [
    {
      "receipt_id": "rkd-line-uuid",
      "qty": 2,
      "occurred_at": "2026-10-01T00:00:00+00:00",
      "line_qty": 5,
      "test_result": "坏品",
      "project_id": "proj-1"
    }
  ],
  "sell_intent": {
    "delivery_id": "550e8400-e29b-41d4-a716-446655440000",
    "unit_id": 42,
    "status": "pending",
    "qty": 2,
    "attempts": 0,
    "block_code": null,
    "last_error_code": null,
    "receipt_status": null,
    "next_attempt_at": null,
    "shop_closeout": null
  }
}
```

`unit_id` 是发给商城的正整数，等于结果的 `wire_unit_id`，不是 UUID。`shop_closeout` 为 `awaiting_shop_reversal` 时，主管用 `delivery_id` 和 `qty` 到商城做盘点减回；其它时候为 null。没有推送时 `sell_intent` 为 null。主管响应仍不含成本、价格、SN、供应商、客户名。`project_id` 只出现在这个明细里。维修员响应不包含 `sell_intent`。

### 4.5 `POST /api/cycle/repair-records/{record_id}/void`

维修员或主管。需要 `Idempotency-Key`。

```json
{
  "expected_version": 1,
  "reason": "填反了",
  "shop_stock_manually_reduced": false
}
```

`reason` 去掉空白后 1–256 字。`shop_stock_manually_reduced` 缺省 false。

一个事务：锁记录与它的意图行；校验版本；按第 5 节决定能否作废；`status=voided` 并写原因、作废人、时间、版本 +1。扣数行保留，但只统计 `status=active` 的结果，所以待修数恢复。未送达且可以确定商城没加库存的意图改为 `failed`，`last_error_code=record_voided`，worker 不再领取。

成功体与 4.4 相同（维修员仍不带来源）。

| 条件 | HTTP | detail |
| --- | --- | --- |
| 记录对当前人不可见 | 404 | 记录不存在 |
| 已作废 | 409 | 该记录已作废 |
| 版本不符 | 409 | 版本冲突：记录已被他人处理，请刷新后重试 |
| 维修员作废，但创建日不是 Asia/Shanghai 的今天 | 403 | 已超过当天，只有主管可以作废 |
| 意图 `delivering` 且租约未过期 | 409 | 推送进行中，请稍后再作废 |
| 第 5 节要求勾选但未勾选 | 409 | 修好数量可能已计入商城库存，请先在商城手工减库存并勾选确认 |
| `missing_operator` 却把勾选打成 true | 409 | 这笔推送还没入账，作废时不要勾选。请等商城补上经办人并盘点减回、核对在手后，再登记 |
| 原因为空或超长 | 422 | 由请求模型产生 |

作废不调用商城，不减少商城库存。

### 4.6 `GET /api/cycle/repair-records/{record_id}/sell-intent`

仅主管。维修员 → 403。记录不可见 → 404。响应等于 4.4 的 `sell_intent`，没有意图时：

```json
{"record_id": "...", "status": "not_queued"}
```

这是现有 `cycle_sell_delivery.read_status` 的记录版。响应含内部 `record_id`、线路 `unit_id`（正整数）和 `qty`。不要把 UUID 放进 `unit_id`。

### 4.7 `GET /api/cycle/summary`

需要 `page_cycle_wip` 或 `page_cycle_manage`。只有维修员键 → 403。同时持有维修员键和采购键时允许。

查询：`q`、`part_ids`（可选，逗号分隔，1–100 个正整数；格式错误 422，detail 沿用现有「part_ids 必须为 1–100 个正整数」）、`page`、`page_size`。未传 `part_ids` 时分页返回三个数里至少有一个大于 0 的 PN。传了 `part_ids` 时按传入顺序返回这些 PN，没有循环数量的行三个数都是 0，不受分页切片（与现有 `/cycle/wip` 一样一次最多 100 个）。

```json
{
  "part_id": 101,
  "pn": "MZ7LH960HAJR-00005",
  "description": "固态硬盘",
  "pending_qty": 4,
  "shop_qty": 2,
  "scrap_qty": 1
}
```

`shop_qty` 是有效结果的修好数，含尚未送达商城的推送。`scrap_qty` 只来自有效结果的废品数，不含件况本身就是废品的返还行。本接口没有推送状态、没有来源。

本阶段前端只在 `/maintenance/cycle` 使用它。型号全景和采购分析不嵌这三列，避免和主库存放在同一屏。

### 4.8 `POST /api/cycle/repair-records/{record_id}/attest-shop-adjustment`

仅主管。需要 `Idempotency-Key`。动作名 `attest_shop_adjustment`，指纹同样包含动作和 `record_id`。

```json
{"expected_version": 2}
```

只用于已经作废、勾选仍为 false、且意图的 `block_code` 是 `missing_operator` 的记录。成功后 `shop_adjusted_manually=true`，版本 +1，`shop_closeout` 变为 null。不改待修数，不调用商城。

| 条件 | HTTP | detail |
| --- | --- | --- |
| 不是这种延迟入库的作废 | 409 | 这笔记录不用登记商城减库存 |
| 版本不符 | 409 | 版本冲突：记录已被他人处理，请刷新后重试 |

## 5. 作废与「商城可能已加库存」

「可能已加库存」为真，当且仅当存在意图，且下面任一成立：

- `state=delivered`，或 `receipt_status` 为 `applied` / `unchanged`
- `block_code=publish_disabled`（v1 里这种 blocked 已经加过库存；商城 v2 不发这个码）
- `attempts > 0` 且状态是 `pending`，或租约已过期的 `delivering`（应答可能丢了）

这里不包括 `missing_operator`。

为真时必须 `shop_stock_manually_reduced=true` 才作废，并把意图写成 `failed` / `record_voided`。

`block_code=missing_operator` 是延迟入库，不是「确定没库存」。作废时意图改为 `failed` / `record_voided`，`shop_stock_manually_reduced` 必须保持 false。主管明细把 `shop_closeout` 标成 `awaiting_shop_reversal`，并显示 `delivery_id`、`unit_id`、`qty`。商城以后保存经办人时会把这行和其它未入账行一起加上库存。主管到商城按商城 v2 §6.2 用盘点减回、核对在手，再调用第 4.8 节把勾选写成 true。作废请求里提前打勾会被拒绝。

「确定未加库存」只有 `missing_catalog_identity`，以及没有意图的纯废品、`attempts=0` 的 `pending`、`failed` 且 `receipt_status` 不是 `applied`。这些作废不要求勾选。`conflict` 不自动重试，作废同样不要求勾选。

租约未过期的 `delivering` 不作废，调用方稍后重试。过期工人的回执 CAS 要求状态仍是 `delivering`，作废已把状态改成 `failed` 时，晚到的回执影响 0 行。

勾选只记录人工声明。itsp 不据此调用商城。

## 6. 出站与回调

出站载荷、回执、HTTP 状态和商城改动见 [v2 增补](cycle-sell-intent-v2.md)。这里固定 itsp 侧的事务边界。

- 意图在提交结果的同一事务插入，`state=pending`。worker 仍是 `SellIntentWorker`：`FOR UPDATE SKIP LOCKED`，租约 60 秒，HTTP 在事务外，回执用租约 CAS。开关关闭时不发 HTTP，意图保持 `pending`。
- 一条有效结果最多一条意图（`record_id` 唯一）。重发使用同一个 `deliveryId`，不新建第二条。
- 商城停机、超时、5xx、409：结果与扣数保持有效，待修数不退回，`shop_qty` 已包含这件。意图保持 `pending` 并退避。主管在明细里看到 `last_error_code`。采购三数不区分是否送达。
- 商城 422：意图 `failed`，不重试，扣数不自动退回。主管作废后待修恢复，再提交会生成新的 `deliveryId` 和新的意图；`wire_unit_id` 仍是原结果上的那个整数，不回收。
- 一次推送的成败只看这一次 POST 的 HTTP 回执。本期商城不调用 requeue。
- 部分成功：数据库事务要么整笔提交（结果 + 全部扣数行 + 至多一条意图 + 幂等行），要么整笔回滚。商城一次投递要么加上全部 `qty`，要么不加。没有「商城只加了一部分」的成功回执。作废要么整笔退回，要么整笔拒绝。

### 6.1 回调

| 方法与路径 | 鉴权 | 本阶段 |
| --- | --- | --- |
| `POST /api/integrations/cycle/requeue` | `CYCLE_SELL_CALLBACK_TOKEN_SHA256`，空则 404 `not_enabled` | 与现实现相同：仅 `blocked` 时把 `available_at` 调到现在 |
| `POST /api/integrations/cycle/mark-sold` | 同上 | 解析通过后 409 `phase_deferred`，零写入 |
| `POST /api/integrations/cycle/revert-sold` | 同上 | 同上 |

`mark-sold` 的 body 仍是 v1：`schemaVersion` 只能为 1，`qty` 只能为 1，`shopOrderLineId` 仍匹配 `^[A-Za-z0-9._:-]{1,64}$`。未知字段 422。不要接受 v2 售出体，避免在延期阶段发明新成功语义。

## 7. 返还行侧的拒绝语义

这些不是新的循环 URL，而是现有写路径必须遵守的同一不变量。合格件数的定义以 D-0058 第 3 节为准。有效扣数 = 该行上 `status=active` 的扣数合计。

| 操作 | 现入口 | 拒绝条件 |
| --- | --- | --- |
| 改小数量、改件况离开坏返、改 `part_id`、作废 | `maintenance_return_receipts.update_receipt` / `void_receipt` | 变更后合格件数 &lt; 有效扣数，或有有效扣数时 `part_id` 发生变化 |
| 入库单重新导入 | `maintenance_return_receipt_import.apply` | 同上。整批拒绝，不写已应用的批次 |

网页改单的 409 `detail` 字符串：「该返还收货已有维修扣数，变更后将少于已扣件数」。重新导入的 `ImportError.code` 为 `repair_deduction_exceeds_receipt`，HTTP 422，消息同一句。应用函数在自己的 `commit` 之前抛出这个错误，避免落到导入 API 的通用 `apply_failed`。

没有有效扣数时，上述修改保持今天的其它校验（容量、版本、项目锁）。件况从空白或成品改为坏返后，行自动进入待修，不调用入池接口。

## 8. 兼容与禁止

- 不静默改 v1 六键的名字。v2 仍用这六个键；`unitId` 改为结果的稳定整数，`qty` 改为 1–1000。`recordId` 不上线路。商城继续接受 v1 的 `qty=1`。
- 用户接口不返回价格、成本、token、SN、供应商、客户名。
- 日志沿用出站现规则：只有 `deliveryId`、`partId`、结果码、是否接受。幂等冲突日志不打 body。
- 页面批量仍不是跨 PN 的数据库事务。一次提交只涉及一个 PN。
