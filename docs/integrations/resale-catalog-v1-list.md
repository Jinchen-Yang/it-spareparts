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
- 游标 id 段只接受 ASCII 十进制且在 1..2^63−1；带符号、下划线、空白、非 ASCII 数字以及换算 UTC 越界的时间（如 `0001-01-01T00:00:00+01:00`）一律 422 `游标无效`。

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
| `updatedAt` | ISO 8601 带时区、微秒精度；D-0049 迁移后，同一 PN 的实际更新严格前进，不是跨 PN 的提交序列 |
| `specs` | `[{key,value,unit,source}]`，按 key 升序；与 `/snapshots` 同形 |

不输出：别名、`exclude_reason`、`data_quality_score`、`reviewed_at`、销售记录数、价格、客户、单号、人员、合同、SN 及任何采购／销售／库存／项目字段。

## 4. 一致性边界（消费者必须知道）

1. 活读，非可重复读快照：翻页期间行被修改可能使其被跳过（随后以新 `updated_at` 再出现）或跨页重复。消费者应用必须幂等。
2. `updated_at` 不是全局提交顺序：INSERT 保留原事务开始时间；长事务在行写入后晚提交，仍可能带有早于消费者当前水位的时间戳，增量拉取会漏掉它。主数据库导入单个文件一个事务，生产实测（2026-10-16 只读查询 `sys_import_job`，306 个已完成作业）单文件作业墙钟上限 244 s（p95 139 s），多文件作业上限 296 s。本版不做回退续传：消费者须定期（商城默认每 24 小时）从头完整遍历对账，漏掉的行最迟在下一次完整遍历时补上。**金臣 2026-10-16 已接受最坏 24h 滞后。** D-0049 修复同一 PN 的版本倒退，不改变跨 PN 活读分页及周期对账边界。
3. 推进 `updated_at` 的写路径：`DimPart.updated_at` 的 `onupdate=func.now()` 对 ORM 实体刷新和 ORM 形式的 `update(DimPart)` 语句都生效，因此人工编辑、合并源／目标、合并路径压缩的子孙行、取消合并、治理排除、改名、`confirm_independent`、`classify_backfill`／`backfill_categories` 都会推进。loader `_upsert_parts` 的 Core upsert 不触发 onupdate，D-36 PR1 起显式写 `CASE WHEN (业务列) IS DISTINCT FROM (当前值) THEN now() ELSE updated_at END`：只有业务列真变化才推进，重导同一文件不推进；墓碑冲突被 WHERE 跳过。原先唯一的缺口是**只写 `product_specs` 的路径**，而 `specs` 是本端点输出字段；D-36 起这些路径同事务推进对应 `dim_part.updated_at`：`refresh_part_specs` 在该型号规格行实际变化时推进；`apply_manual_specs` 只要传入非空修改就推进（即使值相同）；`backfill_specs()` 只推进新增了规格行的型号，`backfill_specs(rebuild=True)` 会把带自动规格的型号全部推进一遍（重建后内容相同也推进，消费者幂等跳过）；`unmerge` 把规格迁回源时推进目标（源由 ORM 状态更新推进）。
4. 主数据库 PN 不物理删除：退出使用只以 merged 墓碑表达。
5. 页大小与体积：默认 200 条；500 条全中文 4000 字描述时可能达约 6 MB，消费者应设置不低于 8 MiB 的响应上限或使用默认页大小。

## 5. D-0049 同一 PN 的技术更新时间约束

迁移 `a3c5e7f9b2d4` 在 `dim_part` 增加 BEFORE UPDATE 行触发器。普通列有实际变化时，`updated_at` 取数据库 `clock_timestamp()`、旧值加一微秒及新提交值三者的最大值。第 4.3 节各入口仍通过 ORM/Core/规格 touch 请求更新，最终时间由数据库统一约束；较早开始、较晚提交的合法编辑不会再把同一 PN 的导出版本倒退。完全无变化的 loader 冲突更新保持原时间。INSERT 不改、迁移不回填现有记录，回滚迁移也不重写任何行。

比较列清单覆盖全部普通列，排除 BEFORE 阶段尚未计算的 `pn_compact/search_doc` 生成列；新增普通列的后续迁移必须同时更新函数，schema 契约防止遗漏。时间类型仍为非空 TIMESTAMPTZ，精度为 PostgreSQL 微秒；NULL 保留原非空约束拒绝，无穷时间或超出可递增范围的更新原子失败，不允许产生不可继续前进的版本。

这不是资料内容的全局 hash：`mergedIntoPnStd` 来自直接父行，父改名不一定推进墓碑自身时间；库存/价格不在本端点，`/snapshots` 的销售聚合可以在 PN 版本不变时独立变化。消费者继续采用同源锁和既有字段规则，拒绝更旧版本，保留相同版本下合法的聚合刷新。

启用新链路前按 [D-0049](https://github.com/Jinchen-Yang/it-spareparts/blob/main/docs/decisions/0029-目录源版本单调与旧快照保护.md) 只读核对同一稳定 partId 的源与镜像版本。历史倒退不通过猜测或批量回填修复；不能解释的源版本落后必须阻断发布。
