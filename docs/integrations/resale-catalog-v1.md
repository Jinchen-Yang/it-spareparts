# 主数据库只读 PN 快照契约 v1

- 日期：2026-10-16；协议版本保持 `schemaVersion: 1`。
- 决策：[D-36](../decisions/0014-主数据库与商城目录互通.md)，含 2026-10-15 18:16Z 金臣裁定 (b) 的销售聚合计数窄例外。
- 范围：既有 `/snapshots` 的兼容扩展；生产者为备件系统主数据库，消费者为商城目录同步服务。本文不表示商城存储、排序、用户验收或部署已经完成。

## 1. 路由、认证与请求

`POST /api/integrations/resale/parts/snapshots`，请求与响应均为 JSON，字段沿用 camelCase。该接口只读，不创建业务记录、库存流水或 AI 研究任务。

使用 `Authorization: Bearer <catalog-service-token>`，由独立配置 `resale_catalog_token_sha256` 校验凭据的 SHA-256。它与返件导出凭据独立，不接受用户登录凭据代替，也不向浏览器分发服务令牌。未配置时返回 HTTP 404 `{"detail":"PN 只读导出尚未启用"}`；缺失或无效凭据返回 HTTP 401 `{"detail":"PN 只读凭据无效"}`。成功响应带 `Cache-Control: no-store`。

```json
{"partIds":[101,101,999]}
```

- `partIds` 是长度 1..100 的整数列表；长度上限在去重前校验，所以 101 个重复 ID 仍超限。
- 每个 ID 必须为正数；空列表、超限或非正数返回 HTTP 422。错误沿用 v1 的 `detail` 形式，不改用 v2 错误信封。
- 按第一次出现的位置去重，响应每个去重后的请求 ID 返回一个 item，顺序保持一致。
- 沿用既有合并解析：active PN 返回自身；merged PN 沿 `merged_into_id` 查找最终节点，响应保留 `requestedPartId`，`partId` 为最终身份，`status` 为 `merged`。不存在、目标缺失、循环或超过 10 次重定向时按 `not_found` 返回，不猜身份。
- 这是按已知身份读取资料的接口，不是增量发布或 v2 固定水位快照。整目录列举见 [resale-catalog-v1-list](resale-catalog-v1-list.md)（`POST /list`，D-36 PR1）。

## 2. 响应字段

成功信封为 `{"schemaVersion":1,"items":[...]}`。既有字段语义保持，所有 item 新增下表两个可空计数字段。

| 字段 | 类型及语义 |
|---|---|
| `requestedPartId` | 请求的正整数 ID。 |
| `partId` | 找到的最终稳定 PN ID；`not_found` item 不含此字段。 |
| `status` | 找到时沿用 PN 状态；经合并重定向时为 `merged`；无法解析时为 `not_found`。 |
| `pnStd` | 最终 PN 的规范名；只用于展示，不用作计数关联键。 |
| `description`, `brand`, `categoryMajor`, `categoryMinor`, `unit` | 既有 PN 通用资料，字符串或 null。 |
| `needsReview`, `isExcluded` | 既有治理标记，布尔值；不决定计数是否纳入。 |
| `lockedFields` | 既有人工保护字段名数组，排序输出。 |
| `updatedAt` | 既有 PN 更新时间或 null；不代表销售计数的更新时间。 |
| `specs` | 既有参数数组；每项为 `key`, `value`, `unit`, `source`，沿用原值。 |
| `salesRecordCount` | 非负整数或 null；null 表示本次没有可信计数。 |
| `salesRecordCountAsOf` | ISO-8601 UTC 时间或 null，格式 `YYYY-MM-DDTHH:mm:ss[.ffffff]Z`；整数计数必须有此时间，本实现未知计数时也返回 null。 |

`not_found` item 仅含 `requestedPartId`、`status` 及两个为 null 的计数字段。成功示例采用合成资料：

```json
{
  "schemaVersion": 1,
  "items": [
    {
      "requestedPartId": 101,
      "partId": 101,
      "status": "active",
      "pnStd": "DEMO-PN-A",
      "description": "合成备件",
      "brand": "Demo",
      "categoryMajor": "硬盘",
      "categoryMinor": "SSD",
      "unit": "块",
      "needsReview": false,
      "isExcluded": false,
      "lockedFields": [],
      "updatedAt": "2026-10-15T08:00:00+00:00",
      "specs": [],
      "salesRecordCount": 2,
      "salesRecordCountAsOf": "2026-10-16T01:00:00Z"
    },
    {
      "requestedPartId": 999,
      "status": "not_found",
      "salesRecordCount": null,
      "salesRecordCountAsOf": null
    }
  ]
}
```

## 3. 销售记录数与失败语义

计数为最终响应 `partId` 对应的**全历史已生效销售订单上的销售明细行数**。按 `f_sales_line.part_id` 聚合，通过 `f_sales_line.order_id` 关联 `f_sales_order.id`，订单条件仅 `data_status='已生效'`。同单同 PN 两行计 2；包含销售换货、零价行、`counts_revenue=false` 及治理排除的 PN。不按数量求和、不去重订单、不限制日期、价格或业务类型，不以 PN 文本或别名猜归属。只统计事实当前绑定的稳定 `part_id`，读取不改写销售事实或身份。

按本批已解析的最终 ID 去重后执行**一条 GROUP BY 查询**；先完整读取结果，再形成所有计数。仅 PN 已存在且整批聚合完整成功时给整数，确无符合行时为可信 `0`。未知不是 `0`：不存在或无法解析的 PN 返回 null；聚合查询失败时，整批 item 的两个计数字段均返回 null，已经可读的 PN 资料仍正常返回。不得采用失败前读到的部分计数，也不向响应回显 SQL、异常内部信息或业务明细。其他资料读取错误仍沿用原接口的错误行为。

成功批次的 `salesRecordCountAsOf` 相同，取**执行聚合查询前记录的 UTC 开始时间**，表示本轮计数观察开始时点。它不是精确数据库事务水位、PN 的 `updatedAt`、订单最后变更时间或跨 PN 资料与销售事实的一致性快照；并发提交可能影响实际查询可见结果。返回后销售事实仍可变化，本接口不承诺跨请求翻页或跨系统实时一致。

## 4. 消费者兼容、刷新与数据边界

旧 v1 客户端可忽略这两个新增响应字段；URL、凭据、请求、旧字段及 `schemaVersion` 保持不变。新客户端必须兼容旧生产者缺失字段的响应：缺失、null、类型无效或身份不能可靠匹配时，均视为没有可信计数并保存 NULL，不能补成 0，也不能从有限的价格证据条数、销量或订单数推算。

商城只将计数作为来源快照的只读元数据，用于**分页前**的资料待办排序：同 PN 好坏件在库合计降序 → 销售记录数降序（NULL 最后）→ 商城 `PN.id` 稳定升序。库存属于商城事实，本接口不提供库存。未取得可信值时自然使用其余排序键，不让未知值排在可信 0 之前。

销售行或订单状态变化不一定修改 PN `updatedAt`；消费者需沿已批准的快照刷新机制重新读取计数，不能只依赖 PN 更新时间。刷新失败不得写入伪 0；如继续保留最后成功来源快照，须保留其原观察时间并标明待刷新，不能标成此次成功值。身份改绑后不能沿用旧身份计数。本次生产者扩展不代表商城已经完成接收、存储与排序。

D-36 的例外仅限上述 v1 `/snapshots` 的聚合计数及观察时点，用途仅为排序。目录负载仍不得带任何销售明细，包括价格、客户、单号、人员、销售行／订单标识，也不得扩大为采购、库存、项目或审批信息。既有独立参考价点击查询不因此成为目录批量取价；计数不进入人工资料命令白名单，不授予服务身份或 AI 写业务状态的权限。

本扩展不向 [catalog v2](catalog-v2.md) 的 node、hash 输入或人工命令添加字段，不改变其版本、发布捕获范围或水位规则。两仓需同步本契约与 v2 兼容说明，并分别验证生产者与消费者；一侧文档或代码更新不证明另一侧已经接通或发布。
