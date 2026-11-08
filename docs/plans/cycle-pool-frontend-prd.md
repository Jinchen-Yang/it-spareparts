# 前端 PRD：坏件循环池与闲鱼可售意图（对接 D-0056）

> 本文件只约束前端产品与对接契约。后端实现见 [cycle-pool-backend.md](cycle-pool-backend.md)；口径真值见 [D-0056](../decisions/0036-坏件循环池与闲鱼可售意图.md)。  
> **本仓库本轮不交付前端代码**；页面由另案设计实现。下列接口以 `/api/cycle` 为准。

## 1. 目标

让回收 / 检测 / 维修 / 主管四类人只在自己的工位工作；采购/售前只看 PN 在制数量；「去电商」后闲鱼侧全自动，前端不做人肉确认上架。

## 2. 角色与页面

| 角色 | 权限键（后端） | 页面职责 |
|---|---|---|
| 回收 | `page_cycle_recycle` | 入池、自测通过、派工、委外、终态选择、报废 |
| 检测 | `page_cycle_test` | 我的待测队列；测好/测坏 |
| 维修 | `page_cycle_repair` | 我的待修队列；修好再测 / 不测进终态 / 修不了报废 |
| 主管 | `page_cycle_manage` | 全量检索、委外供应商、事件时间线、售出标记 |
| 采购/售前读 | `page_cycle_wip` | 按 PN 看在测/在修/委外/待上架数量 |

菜单与维保 `page_maintenance` **脱钩**。检测/维修账号即使有管理员壳，也不得在 UI 展示项目名、客户、委外供应商。

## 3. 信息架构（建议）

1. **回收工作台**：待入池提示（可选，从返还收货跳转）+ 回收池列表 + 派工/自测/终态抽屉  
2. **检测工作台**：仅 `station=testing` 且派给我（或未派时公共池，若产品要公共池需另开口径；后端现支持按 assignee 过滤）  
3. **维修工作台**：仅 `station=repairing`  
4. **主管台**：多工位筛选 + 供应商列 + 事件  
5. **在制只读条**：嵌在采购/售前 PN 旁，调 `GET /cycle/wip`，**不要**改型号全景页

不要求本迭代做：工单、测报 PDF、SN 扫描主路径、闲鱼后台嵌页。

## 4. 核心用户流

### 4.1 入池

- 从返还收货行：选正整数件数 → `POST /cycle/units/handoff-from-receipt`  
  body：`receipt_id`、`qty`、`expected_receipt_version`、可选 `serial_numbers[]`  
- 外部货：`POST /cycle/units/intake-external`（`part_id` 或可解析 `pn`、件数、备注）  
- 成品也可入池；入池后可直接终态。

### 4.2 回收池操作

| 操作 | 接口 |
|---|---|
| 派检测/维修/委外 | `POST .../dispatch` `{to, assignee_username?, vendor_name?, expected_version}` |
| 自测为好 | `POST .../self-pass` → 进入 `ready` |
| 直接报废 | `POST .../close` `{outcome:"scrap"}` |
| 终态（ready） | `POST .../close` `{outcome, condition_bucket?}` |

委外必须填 `vendor_name`（仅管理/回收写；检测维修 UI 永不展示）。

### 4.3 检测

`complete-test`：`{result:"good"|"bad", next:"ready"|"repairing"|"scrap", expected_version}`  
- good → next 必须 `ready`  
- bad → `repairing` 或 `scrap`

### 4.4 维修

`complete-repair`：`{result:"fixed"|"unfixed", next:"testing"|"ready"|"scrap", expected_version}`  
- fixed + testing = 再测  
- fixed + ready = 不测直接终态  
- unfixed → scrap

### 4.5 委外回来（回收/主管）

`outsource-return`：手选 `next` ∈ `testing|repairing|to_shop|to_company|scrap`。  
选 `to_shop` 时必填 `condition_bucket`。

### 4.6 去电商 / 回公司

- `to_shop`：前端只提示「已提交自动上架」；**无草稿确认页**  
- `to_company`：可填交接备注；明确文案「不写氚云库存」  
- 售出一般由商城回写；主管可 `mark-sold` 兜底

## 5. API 契约摘要（camelCase 响应建议；后端若 snake_case 以 OpenAPI 为准）

### Unit 列表项（工人）

```json
{
  "id": 1,
  "unitUid": "...",
  "partId": 12,
  "pn": "PN-X",
  "station": "testing",
  "outcome": null,
  "conditionBucket": null,
  "intakeCondition": "坏品",
  "repairRound": 0,
  "assigneeUsername": "zhangsan",
  "version": 3,
  "updatedAt": "..."
}
```

工人响应**不得**含：`vendorName`、`sourceReceiptId`、`projectId`、`projectName`、客户字段。

### WIP

`GET /cycle/wip?part_ids=1,2,3`

```json
{
  "items": [
    {
      "partId": 1,
      "testing": 2,
      "repairing": 1,
      "outsource": 0,
      "ready": 3
    }
  ]
}
```

### 错误

| HTTP | 含义 |
|---|---|
| 400 | 非法转移 / 缺 condition_bucket |
| 403 | 无页键或工人越权看管理字段 |
| 404 | 单位不存在 |
| 409 | 版本冲突或收货行已无法入池 |

所有写操作带 `expected_version`；成功后以响应内新 `version` 为准。

## 6. 文案与合规

- 闲鱼/对外帖：前端配置与预览若有，禁止展示「维修」「翻新」「坏件来源」「SN」。  
- 内部工位可以用「送修」「委外」。  
- 一类一贴：UI 说明「同型号同成色合并为一帖加库存」。

## 7. 非目标

- 不实现商城 / 闲管家控制台  
- 不改造维保返还登记主流程（只增加「交接入池」入口）  
- 不把在制数塞进型号全景  
- 不做 SN 强制采集

## 8. 验收清单（前端）

- [ ] 四种页键账号登录后菜单与默认路由正确；互串 403  
- [ ] 检测账号 Network 面板响应无 vendor/项目字段  
- [ ] 修好「再测 / 不测」两个按钮都可用  
- [ ] 委外回来五个去向都可点；去闲鱼要选成色桶  
- [ ] 去电商无二次「确认上架」弹窗（至多成功 toast）  
- [ ] WIP 组件只读、按 part_id 批量、空态不显示 0 伪装未知（无权限时整块隐藏）
