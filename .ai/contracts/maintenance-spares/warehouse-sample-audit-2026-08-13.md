# Warehouse Real-Sample Data Quality Audit

审计日期：2026-08-13（Asia/Shanghai）

用途：判断用户提供的 S07 发货、S08 退货返库、S09 入库样表是否足以冻结 G1a 仓库适配合同。审计只读取双表头、稳定键质量、状态聚合、日期范围、公式/合并结构和 SHA；未在本文记录客户、PN、SN、金额、人员或业务行。

## 结论

- 三类宽表都被现有字段选择器正确识别：`shipment_v1`、`return_v2`、`receipt_v1`。
- S07/S09 不再是“没有真实样表”，而是“真实样表已收到、现 parser 无法安全完整处理”。
- 当前不能 production apply：大表含少量公式、超过 500 万 cell 全局限制，并以纵向合并表达一头多行；现 parser 会整本拒绝或漏掉续行。
- S08 窄表 `return_v1` 没有 header/line stable ID，只能作为历史结构参考；宽表 `return_v2` 才可能成为未来权威外部返库源。
- 费用报销样例归 S06 后置来源，不阻塞备件首版。

## 数据集与粒度

| 来源 | 协议 | 表头列 | 业务头 | 明细行 | 日期覆盖 | 合同用途 |
|---|---|---:|---:|---:|---|---|
| S07 | shipment_v1 | 166 | 19,572 | 69,298 | 2026-01-01—2026-08-06 | 来源库→在途 |
| S08 宽表 | return_v2 | 120 | 3,019 | 7,104 | 本轮未用于核心 Gate | optional 外部返还对账 |
| S08 窄表 | return_v1 | 49 | 无稳定头 ID | 27 | 本轮未用于核心 Gate | 非权威历史参考 |
| S09 | receipt_v1 | 155 | 10,177 | 82,911 | 2026-01-01—2026-08-06 | 正式入库证据 |

## 高优先级发现

### Critical：现 parser 不支持真实宽表规模

- S07 主表 declared dimension 为约 1,150 万个矩形 cell slots；由于结构扫描超过 240 秒，实际物理 `<c>` 节点数未知。S09 实测物理 `<c>` 节点同样超过当前 `IMPORT_XLSX_MAX_DECLARED_CELLS=5,000,000`。
- 现 API 虽有 100 MB 上传限制、512 MB 解压限制、单解析 semaphore，但 cell 限制会在合法真实导出中触发。
- 影响：即使公式被固化，真实文件仍无法 preview/apply。
- 修复：G1a 增加仓库专用受控预算；不得放宽通用 Excel importer。解析只投影批准的 typed fields，其他字段只进入摘要，不保留全行 payload。

#### 2026-08-14 只读结构 benchmark

- S09 主 sheet：`501,666,456` XML bytes、`12,851,515` 个物理 `<c>`、`641,350` 个 merge ranges、`7,346,134` 个 merge 继承跨度、`270` 个 formula nodes。单遍 `ElementTree.iterparse` 仅统计结构耗时 `13.10s`，peak RSS `77,872 KiB`；未输出业务值。
- S07 同类单遍结构统计超过 `240s` 硬门并由 Root 终止，未形成可信完整计数。该结果按性能门禁失败处理，不能凭 S09 的表现推断 S07 可接受。
- 因此 `20,000,000 cell / 2,000,000 merge / 8,000,000 inherited span` 只是 parser sandbox 的拒绝上限，不是发布容量证明。真实 S07 必须先通过优化后的 ≤180s / peak RSS delta ≤768MiB benchmark；否则保持失败关闭。

### Critical：纵向合并导致现 parser 漏明细

- S07 有 49,726 条明细行没有物理重复 header ID；S09 有 72,734 条。
- 这些行位于已有单据头之后，符合氚云“一头多明细纵向合并”导出形态；现 parser 却要求每个物理行都有 `ObjectId/SeqNo`，会把续行记为 missing document ID 并跳过。
- 修复：只在受验证 merge range 内继承 header facts；普通空白不得盲目 fill-forward。document ID、SeqNo 与状态/日期必须来自同一已验证头范围。

### High：公式是导出辅助证据，不是业务事实字段

- S07 只有 2 个公式 cell，均在图片/附件字段。
- S08 宽表有 95 个公式 cell，集中在一个非 typed optional 列。
- S09 有 270 个公式 cell，位于附件、图片/附件和测试报告字段。
- 现 parser 对任意公式整本拒绝，导致真实样表失败关闭。
- 修复：双表头获批后，只有 metadata 明确标记为 controlled attachment/evidence 的列可接受公式；不求值、不读取缓存值，只记录 controlled marker/digest。稳定键、状态、日期、数量、PN/SN、仓库、库位、检测结果出现公式仍整本拒绝。

### High：S09 仍需行级 ambiguity，而非整本伪通过

- 82,911 条数据行中，稳定 line ID 有 82,910；数量有 82,906；PN 有 82,879。
- 这说明主合同可成立，但少量行必须进入 ambiguity queue，不能静默丢弃或把整本称为 100% 完整。
- S09 检测结果存在 3 个原值，仓库/库位也存在稳定 ObjectID 字段；“哪个测试结果可恢复可用库存”仍需锁定枚举语义。

## 稳定键与状态证据

### S07

- header `ObjectId`：19,572 个，distinct 19,572。
- `SeqNo`：19,572 个，distinct 19,572。
- line ObjectId：69,298 个，distinct 69,298。
- ObjectId↔SeqNo 冲突：0。
- 状态头：`已生效=19,570`、`草稿=2`。
- 候选映射：`已生效→confirmed`、`草稿→pending`、`已取消→void`；未知值 blocked。

### S08

- return_v2 line ID：7,102 个 distinct，2 行物理缺失。
- 状态头：`已生效=2,996`、`已取消=9`、`草稿=14`。
- return_v1 不含 header/line stable ID，不得成为权威源。
- 两模板的 49 个公共字段标签完全一致；窄表 27 行都能在宽表公共字段投影中找到，但这不证明 revision/supersedes 关系。

### S09

- header `ObjectId`：10,177 个，distinct 10,177。
- `SeqNo`：10,177 个，distinct 10,177。
- line ObjectId：82,910 个，distinct 82,910；1 行缺失。
- ObjectId↔SeqNo 冲突：0。
- 状态头：`已生效=10,107`、`草稿=5`、`已取消=65`。
- 候选映射：`已生效→confirmed`、`草稿→pending`、`已取消→void`；未知值 blocked。

## G1a 自动化测试要求

1. exact header signature 与 required internal code 测试；原始附件不进入 Git。
2. 仅 controlled attachment/evidence 列可含公式；typed fact 公式必须失败关闭。
3. merge-aware header inheritance 只在显式 merge range 内生效；普通空值不继承。
4. 真实规模预算测试：S07/S09 能 preview，超过仓库专用上限仍返回 413；通用 importer 上限不变。
5. typed projection 与 whole-row canonical digest 分离，避免 1,000 万 cell payload 常驻内存。
6. stable ID not-null/unique、header↔number、line capacity、unknown status、少量坏行 ambiguity 测试。
7. `source stable ID + canonical digest + mapping version` 幂等；相同内容重传不建新 revision，内容变化进入 supersession/ambiguity。

## Gate 判定

- G1a-P parser sandbox 本地 TDD：可以启动。
- S07/S09 production apply：仍为 false。
- G1a-R relation/delivery、G2 production schema、Lane A–C、部署：仍受整体 G0、代码审查和发布门禁约束。
