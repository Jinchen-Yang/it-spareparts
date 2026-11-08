# 循环可售意图投递契约 v1（草案）

- 日期：2026-11-07（America/Los_Angeles）。
- 决策：D-0057。
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
| `unitId` | 正整数；循环单位 id。商城队列与在架链接按它绑定具体一件，售出回写也按它；不是排队键 |
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
| `applied` | 首次已把该 unit 放入同键可售队列；是否立即挂出取决于该键当前有无在架链接 | `delivered` |
| `unchanged` | 同 `deliveryId` 幂等重放，未重复入队 | `delivered` |
| `blocked` | 业务可恢复阻点（缺正式图、缺类目映射等）；**不要**当传输失败无限狂重试同一秒 | `blocked` |
| `conflict` | 身份/人工保护等需人处理；默认不自动重试 | 映射 `failed`（或表扩枚举后独立态） |

额外键、类型错误、错 `deliveryId`/`partId`/`unitId`、未知 `status` → 视为无效回执，itsp 可重试，不得标 delivered。

`blocked` 时允许附加：

```json
"blockCode": "missing_formal_image" | "missing_category_mapping" | "other",
"blockMessage": "短文本，无密钥无堆栈"
```

## 4. 商城处理语义

1. 以 `deliveryId` 幂等；同 id 重放不得重复入队。  
2. 排队键：`(partId, conditionBucket)`。每键在闲鱼同时最多一条在架链接，链接库存恒为 1，绑定队首 unit；经闲管家自动发布，员工不手动发布。  
3. 在架链接售出后，商城按该 unit 回写 itsp `sold`；同键队列仍有 unit 时自动新挂一条，队列空则不挂。  
4. 不在本接口创建主数据 PN；目录缺失时返回 `blocked`（`blockCode` 可另定）或 `conflict`，由目录同步先行。  
5. 入队（`applied`）之后的闲管家发布失败由商城内部重试并告警，不回改 itsp 意图态；itsp 只关心入队与按 unit 的售出回写。  
6. 对外文案不得含维修来源、SN、内部项目信息。

## 5. 售出回写（配套，可同版或 +0.1）

商城在绑定该 unit 的在架链接售出后：

- 调用 itsp `POST /api/cycle/units/{unitId}/mark-sold`（服务凭据版待加），或  
- 批量对账接口（后续另文）。

售出不写 itsp 变卖台账、不建闲鱼售后闭环（D-0057）。

## 6. 验证建议

- 契约测试：敏感字段拒绝；幂等不重复入队；blocked 不标 delivered。  
- 联调：缺图 → blocked → 补图 → 再投 delivered；同键两件只挂一条，首件售出回写 sold 后自动挂第二件；氚云库存不变。
