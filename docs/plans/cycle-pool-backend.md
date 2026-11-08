# 坏件循环池 · 后端方案（D-0057）

决策: D-0057  
范围：本仓库后端（模型 / 迁移 / 服务 / HTTP / 可售意图出站）。不含前端页面、不含商城闲管家实现。

## 1. 表

### `maintenance_cycle_unit`

| 列 | 说明 |
|---|---|
| `id` | BIGSERIAL PK（供后续待办 subject 挂接） |
| `unit_uid` | UUID 对外稳定 ID |
| `part_id` | FK `dim_part.id` NOT NULL |
| `pn` | 入池 PN 快照 |
| `station` | `pool\|testing\|repairing\|outsource\|ready\|closed` |
| `outcome` | NULL 或 `to_shop\|to_company\|scrap\|sold` |
| `condition_bucket` | NULL 或 `good\|bad`（去电商时必填） |
| `intake_condition` | 入池件况快照（成品/坏品/废品/空） |
| `repair_round` | 维修次数，默认 0 |
| `source_kind` | `receipt\|external` |
| `source_receipt_id` | 可空 → `maintenance_rkd_return_line.rkd_line_id` |
| `serial_number` | 可空；仅管理可见 |
| `vendor_name` | 可空；仅管理可见 |
| `assignee_username` | 可空 |
| `note` | 可空 |
| `version` | CAS ≥1 |
| `created_by` / `updated_by` / 时间戳 / `closed_at` |

### `maintenance_cycle_event`

追加事件：`event_type` + `payload` JSONB + `actor` + `unit_id`。不改历史。

### `maintenance_cycle_sell_intent`

| 列 | 说明 |
|---|---|
| `delivery_id` | UUID PK |
| `unit_id` | FK unit |
| `part_id` | |
| `qty` | 固定 1（单位行） |
| `condition_bucket` | good/bad |
| `state` | `pending\|delivering\|delivered\|blocked\|failed` |
| `attempt_count` / `last_error` / `lease_*` | 出站投递 |

## 2. 状态机（服务层唯一入口）

```
handoff/intake → pool
pool → testing | repairing | outsource | ready | closed(scrap)
pool.self_pass → ready
testing.complete → ready | repairing | closed(scrap)
repairing.complete → testing | ready | closed(scrap)
outsource.return → testing | repairing | closed(to_shop|to_company|scrap)
ready.close → closed(to_shop|to_company|scrap)
closed(to_shop) + shop sold → outcome=sold
```

每次转移：`expected_version` CAS；写一条 event；成功 `version+1`。

## 3. HTTP（前缀 `/api/cycle`）

| 方法 | 路径 | 权限 |
|---|---|---|
| POST | `/units/handoff-from-receipt` | recycle |
| POST | `/units/intake-external` | recycle/manage |
| GET | `/units` | 按页键过滤工位 |
| GET | `/units/{id}` | 可见范围内 |
| POST | `/units/{id}/dispatch` | recycle/manage |
| POST | `/units/{id}/self-pass` | recycle |
| POST | `/units/{id}/complete-test` | test |
| POST | `/units/{id}/complete-repair` | repair |
| POST | `/units/{id}/outsource-return` | recycle/manage |
| POST | `/units/{id}/close` | recycle/manage |
| POST | `/units/{id}/mark-sold` | manage（或服务凭据后续） |
| GET | `/units/{id}/events` | manage 全量；工人精简 |
| GET | `/wip` | wip 页键；`part_ids` 批量 |

工人序列化白名单：不含 `vendor_name`、`source_receipt_id`、项目字段。

## 4. 可售意图

`close(to_shop)` 同事务 `enqueue` 意图。出站进程（可与 catalog_delivery 同模式）投递商城；未配置商城 URL 时保持 `pending`，不伪造成功。payload camelCase，对齐目录投递风格，必带 `unitId`。

商城侧语义（决定第 7 条）：意图投递只表示「这件进入商城可售队列」。商城用闲管家自动发布；同 `(partId, conditionBucket)` 同时最多一条在架链接、库存 1，链接绑定队首 unit；售出后回写该 unit `sold`，同键还有排队件就自动新挂一条。本仓不感知闲鱼商品 ID，只接收按 `unitId` 的售出回写。

## 5. 明确不做（本 PR）

- 前端页面与路由
- 闲管家密钥与发布逻辑（商城仓）
- work-items 注册（预留 `id`；本轮用 `assignee_username` + 列表）
- 氚云任何写回
- 改 `/bad-receipts` 语义
