# 主数据库只读整目录列举契约 v1（`/list`）

- 日期：2026-10-16；协议版本 `schemaVersion: 1`。
- 决策：[D-36](https://github.com/Jinchen-Yang/it-spareparts/blob/main/docs/decisions/0014-主数据库与商城目录互通.md)（金臣 2026-10-16 批准实施：每个主数据库 PN 自动在商城存在为 0 库存商品，墓碑与合并镜像）。
- 生产者：备件系统（itsp）主数据库；消费者：商城目录导入任务。两仓保存本文相同副本（itsp `docs/integrations/resale-catalog-v1-list.md`，商城 `docs/integrations/resale-catalog-v1-list.md`），修改须两边同步。
- 性质：**v1 过渡读面，活读当前 `dim_part`**。它不是 [catalog v2](catalog-v2.md) 的固定快照、发布水位、增量批或 hash；v2 仍为拟定稿，未实现。

## 1. 路由与认证

`POST /api/integrations/resale/parts/list`，JSON，camelCase。只读：不写业务数据，不触发 AI／研究。

沿用 resale v1 的同一凭据 `resale_catalog_token_sha256`（Bearer）：未配置 404 `{"detail":"PN 只读导出尚未启用"}`；缺失、错误或返件导出凭据 401 `{"detail":"PN 只读凭据无效"}`。成功响应带 `Cache-Control: no-store`。错误沿用 v1 `detail` 形式。

请求：

```json
{"cursor": "<上一页 nextCursor，首页省略>", "limit": 200}
```

- `limit` 1..500，默认 200；`cursor` 最长 512 字符；未知字段、越界或非法游标返回 422（游标非法 detail 为 `游标无效`）。
- 游标是不透明字符串（内部编码 `(updated_at 微秒精度 UTC, id)`），消费者不得解析或构造。

## 2. 排序、分页与游标

- 按 `(updated_at, id)` 升序，行值比较 keyset 分页；同一事务写入、`updated_at` 完全相同的行由 `id` 决胜，不漏不重。
- 响应：`{"schemaVersion":1,"items":[...],"hasMore":bool,"nextCursor":string|null}`。
- **`nextCursor`：本页 `items` 非空时总是本页最后一条的游标（即使 `hasMore=false`）；`items` 为空时为 `null`。** `hasMore` 只表示是否还有后续行。消费者追平末尾后保存最后一个非空页的游标，下次以它请求，只得到之后变更的行；空页表示暂无变化，保留原水位，不得把水位清空。
- 越过末尾的游标返回空页，不是 404。

## 3. item 字段

| 字段 | 说明 |
|---|---|
| `partId` | 稳定身份 `dim_part.id` |
| `pnStd` | 主数据库规范 PN（`standardize_pn`：大写，遇 V 码 token 截断其后） |
| `status` | `active` 或 `merged`（行自身状态，不改写成最终身份） |
| `mergedIntoId` / `mergedIntoPnStd` | 仅 merged 非空：**一跳**直接父行的 id 与 pn_std（A→B→C 时 A 指向 B）。合并路径压缩后链长通常为 1，消费者不得自行猜根 |
| `machineOrPart` | 类型原值或 null |
| `description` | 服务端截断至 4000 字符；null 保持 null |
| `brand`、`categoryMajor`、`categoryMinor`、`unit` | PN 文本列原值或 null |
| `needsReview`、`isExcluded` | 治理标志；**全部状态均输出**，待审与排除不影响商城建品（D-36 第 3 条） |
| `lockedFields` | 排序后的字符串数组 |
| `updatedAt` | ISO 8601 带时区 |
| `specs` | `[{key,value,unit,source}]`，按 key 升序；与 `/snapshots` 同形 |

不输出：别名、`exclude_reason`、`data_quality_score`、`reviewed_at`、销售记录数、价格、客户、单号、人员、合同、SN 及任何采购／销售／库存／项目字段。

## 4. 一致性边界（消费者必须知道）

1. 活读，非可重复读快照：翻页期间行被修改可能使其被跳过（随后以新 `updated_at` 再出现）或跨页重复。消费者应用必须幂等。
2. `updated_at` 是事务开始时间：长事务晚提交的行可能带有早于消费者当前水位的时间戳。消费者须定期（商城默认每 24 小时）从头完整遍历对账。
3. 推进 `updated_at` 的写路径：ORM 更新（人工编辑、合并源／目标、治理排除、改名）和 loader `_upsert_parts` 冲突更新（D-36 PR1 起显式 `updated_at=now()`，墓碑冲突被跳过不推进）。**暂不推进**的 Core 写路径：`classify_backfill`（品类回填）、`confirm_independent`（needs_review）、合并路径压缩的子孙行、规格回填 `product_specs`。这些变化只能靠定期完整遍历发现；是否补齐列为待金臣拍板的后续项。
4. 主数据库 PN 不物理删除：退出使用只以 merged 墓碑表达。
5. 页大小与体积：默认 200 条；500 条全中文 4000 字描述时可能达约 6 MB，消费者应设置不低于 8 MiB 的响应上限或使用默认页大小。
