# 循环可售意图投递契约 v1（草案）

- 日期：2026-11-07（America/Los_Angeles）。
- 决策：D-0056。
- 状态：**拟定**。生产者 = 备件系统（itsp）出站 worker；消费者 = 商城（spareparts-shop）。本文不表示商城已实现或已上线。
- 与目录投递关系：目录同步只保证 PN/SKU 资料与零库存壳；**本契约单独驱动二手可售数量与闲鱼上架**。二者 token、URL、回执表均独立。

## 1. 路由、认证与传输

`POST /api/integrations/cycle/sell-intents`（商城侧路径建议名；最终以 shop 立项后冻结为准）。

- JSON，字段 camelCase。
- `Authorization: Bearer <cycle-sell-service-token>`；itsp 配置明文 token，商城存摘要或对等密钥。  
  **不得**复用：用户 JWT、目录 sync token、图库 token、返件导出 token。
- 超时：客户端总网络期限 15s；不跟随 redirect；不读取 HTTP(S)_PROXY 环境。
- 响应体上限 4KiB；超限视为 `invalid_receipt`，可重试。

未启用（itsp `SHOP_CYCLE_SELL_ENABLED=false` 或 URL 未配）：意图保持 `pending`，**不**伪造成功。

## 2. 请求

```json
{
  "schemaVersion": 1,
  "deliveryId": "550e8400-e29b-41d4-a716-446655440000",
  "unitId": 42,
  "partId": 101,
  "conditionBucket": "good",
  "qty": "1"
}
```

| 字段 | 约束 |
|---|---|
| `schemaVersion` | 固定 `1` |
| `deliveryId` | UUID；itsp 出站主键；幂等键 |
| `unitId` | 正整数；循环单位 id（商城可存映射，勿当合帖键） |
| `partId` | 正整数；稳定 PN 身份 |
| `conditionBucket` | `good` \| `bad` |
| `qty` | 十进制字符串或数字，必须 `> 0`；当前 itsp 一单位一行，qty 恒为 1 |

禁止字段（出现即 `invalid_receipt` / 拒绝）：项目、客户、SN、供应商、维修备注、成本、价格、人员。

## 3. 成功回执

HTTP 200，JSON：

```json
{
  "schemaVersion": 1,
  "deliveryId": "550e8400-e29b-41d4-a716-446655440000",
  "partId": 101,
  "unitId": 42,
  "status": "applied"
}
```

| `status` | 语义 | itsp 意图态 |
|---|---|---|
| `applied` | 首次（或等价）已按合帖键增加可售库存，并已尝试自动上架/加库存 | `delivered` |
| `unchanged` | 同 `deliveryId` 幂等重放，不再加库存 | `delivered` |
| `blocked` | 业务可恢复阻点（缺正式图、缺类目映射等）；**不要**当传输失败无限狂重试同一秒 | `blocked` |
| `conflict` | 身份/人工保护等需人处理；默认不自动重试 | 映射 `failed`（或表扩枚举后独立态） |

额外键、类型错误、错 `deliveryId`/`partId`/`unitId`、未知 `status` → 视为无效回执，itsp 可重试，不得标 delivered。

`blocked` 时允许附加：

```json
"blockCode": "missing_formal_image" | "missing_category_mapping" | "other",
"blockMessage": "短文本，无密钥无堆栈"
```

## 4. 商城处理语义

1. 以 `deliveryId` 幂等；同 id 重放不得双倍加库存。  
2. 合帖键：`(partId, conditionBucket)` → 一条 listing；库存 `+= qty`（仅 `applied` 路径首次）。  
3. 不在本接口创建主数据 PN；目录缺失时返回 `blocked`（`blockCode` 可另定）或 `conflict`，由目录同步先行。  
4. 闲管家发布失败若属凭证/配额等可恢复问题，可用 `blocked`；明确不可恢复用 `conflict`。  
5. 对外文案不得含维修来源、SN、内部项目信息。

## 5. 售出回写（配套，可同版或 +0.1）

商城在对应 unit 所贡献库存售出后：

- 调用 itsp `POST /api/cycle/units/{unitId}/mark-sold`（服务凭据版待加），或  
- 批量对账接口（后续另文）。

售出不写 itsp 变卖台账、不建闲鱼售后闭环（D-0056）。

## 6. 验证建议

- 契约测试：敏感字段拒绝；幂等不加双倍；blocked 不标 delivered。  
- 联调：缺图 → blocked → 补图 → 再投 delivered；氚云库存不变。
