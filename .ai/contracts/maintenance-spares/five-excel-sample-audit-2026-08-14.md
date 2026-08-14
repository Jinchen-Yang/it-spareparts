# 五份真实 Excel 只读数据质量审计（2026-08-14）

> 结论：附件足以作为真实 `observed/candidate` 样本并驱动 parser TDD，但没有任何一份达到 authoritative。
> 隐私：只记录 SHA、结构、计数、日期范围、哈希化状态桶与精确匹配覆盖；未记录或输出业务行、客户、人员、PN、SN、金额或附件内容。

## 1. 文件级结论

| 来源 | SHA-256 | 结构 | 文档/明细 | 稳定键质量 | 日期范围 | formula / merge | 等级 |
|---|---|---|---:|---|---|---:|---|
| S06 费用 | `02d956ea9cac2d19ecd96b11d020360cecd28dacde9871a14d4b87fadee53517` | 1 visible；43 列；2 行表头 | 15 / 97 | header ID、单号、line ID 均 0 null/dup | 2026-02-14—2026-07-31；1 个头缺日期 | 0 / 0 | missing-contract |
| S07 发货 | `993ce190b1572528585417be58a60be096d395fac340bf8eef62954b6b0042b8` | 1 visible + 8 veryHidden；166 列 | 19,572 / 69,298 | header ID、单号、line ID 均 0 null/dup | 2026-01-01—2026-08-06 | 2 / 1,018,932 | candidate-only |
| S08 窄表 | `0e49cd36ff047d9dcfce6263c7996d50572d96fa76177462fb27fb2ba372319f` | 1 visible + 8 veryHidden；49 列 | 10 个推断头 / 27 | 没有稳定 header/document/line ID | 2026-03-09—2026-08-03 | 0 / 120 | non-authoritative |
| S08 宽表 | `7485f5848a4c08fc6844645ff2854c4eac9c38f41fe099aa4d8c06f04987351b` | 1 visible + 10 veryHidden；120 列 | 3,019 / 7,104 | header ID/单号 0 null/dup；line ID 2 null、0 dup | 2026-01-04—2026-08-06 | 95 / 68,680 | candidate-only optional |
| S09 入库 | `d11538c7a23ba3f6ae8bcca339a37a1a64aa9eabf61955c7e1f16d152c000032` | 1 visible + 11 veryHidden；155 列 | 10,177 / 82,911 | header ID/单号 0 null/dup；line ID 1 null、0 dup | 2026-01-01—2026-08-06 | 270 / 641,350 | candidate-only |

S07 主表完整流扫观察到 `11,503,800` 个物理 cell；S09 为 `12,851,515`。两者均超过通用 importer 的 500 万 cell 上限，必须使用 warehouse-only streaming parser 和独立预算，不能放宽通用导入器。

## 2. 状态证据

- S07 candidate normalized 聚合：confirmed 19,570，pending 2。
- S08v2 candidate normalized 聚合：confirmed 2,996，pending 14，void 9。
- S08v1 candidate normalized 聚合：confirmed 10；但没有稳定身份，仍 non-authoritative。
- S09 candidate normalized 聚合：confirmed 10,107，pending 5，void 65。
- S06 观察到两个原始状态桶：14 和 1；本文只保存哈希桶，不把未批准的原值映射为 approved/rejected。

上述均是候选映射。source owner 未批准前 `approved_mapping={}`、`production_apply_allowed=false`。

## 3. Merge empty-anchor 证据

格式为 `empty anchor / monitored merged ranges`。

- 身份、状态、日期：S07 `0/9348`，S08v2 `0/808`，S09 `0/6350`。这些字段的空 anchor 必须整本失败关闭。
- S07 optional refs：`F0000033 3910/9348`、`F0000064 3779/9348`、`F0000147 8889/9348`、`F0000151 5958/9348`、`F0000192 5948/9348`。
- S08v2 optional refs：`F0000033 279/808`、`F0000064 274/808`、`F0000139 616/808`、`F0000146 85/808`、`F0000156 616/808`、`F0000165 0/808`、`F0000166 808/808`。
- S09 optional refs：`F0000178 0/6350`、`F0000179 6350/6350`、`F0000142 6299/6350`、`F0000147 1689/6350`。
- S08v1 status/date 为 `0/3`；optional `F0000064/F0000166` 为 `3/3`，但该版本仍因无稳定 ID 永不 authoritative。

Parser 规则：

- 只有显式纵向 merge 才允许 header fact 继承；普通空白永不 fill-forward。
- ObjectId/SeqNo identity merge 的 top-left 必须非空且非公式。
- optional/conditional fact 的显式 merge 若 top-left 为空，整个 range 保持 NULL；不得从上一单据泄漏旧值。
- merge continuation 子格存在物理值时失败关闭。

## 4. 跨文件严格匹配

仅执行 NFKC/空白归一后的 whole-cell exact match；没有拆词、模糊匹配或 AI 推断。

- S07 header receipt reference → S09 document no：`81/523 = 15.49%` 文档覆盖；distinct `80/518 = 15.44%`。
- S07 line receipt reference → S09 line ID：`0/1,565` occurrence；`0/280` distinct。
- S08v2 通知引用非空分母为 0，S08→S09 为 N/A。
- S08v1 的 27 行在 S08v2 的 49 个公共字段投影中 `27/27` 命中；只能证明样本内容覆盖，不能证明 revision/supersedes 或稳定身份。

因此：

- 不能用 PN+日期连接 S07/S09；
- header reference 的 15.49% 覆盖不足以自动建立全量正式链；
- zero/multi-match 必须进入 ambiguity；
- S09 GoodReturn allocation 需要正式、稳定、行级关系合同。

## 5. ParentIndex 与 S09 来源引用证据

`ParentIndex` 仅按显式 document merge 边界聚合，普通空白未做 fill-forward；未记录任何行值：

- S07：69,298/69,298 为整数，范围 1–163；19,572 个 document 全部严格为 `1..N`，无 null、重复、gap、非单调或非正值。
- S08v2：7,102/7,104 为整数，范围 1–74；2 行 null，分别使 2 个 document 不满足完整 `1..N`；其余 3,017 个 document 严格连续，无其他异常。
- S09：82,910/82,911 为整数，范围 1–160；1 行 null，使 1 个 document 不满足完整 `1..N`；其余 10,176 个 document 严格连续，无其他异常。

建议：candidate/raw fact 使用 nullable `INTEGER CHECK (line_no BETWEEN 1 AND 2147483647)`；null/invalid 只产生 line-scoped ambiguity，绝不回退到 Excel 物理行号。ready/authoritative gate 另要求 `NOT NULL + UNIQUE(document,line_no) + document 内严格 1..N`。

S09 三个上游引用的 presence bit 顺序为 `F0000179/F0000178/F0000147`：document 仅出现 `010=4,399` 与 `011=5,778`，line 仅出现 `010=12,381` 与 `011=70,530`；`F0000179` 在本样本中始终为空。该集合事实不能推导来源类型，三个引用必须独立保存；`receipt_origin_raw` 候选来自 `F0000032 / 入库类别(必填)`，只有 owner 批准 raw→normalized mapping 后才能生成 `receipt_origin_kind`。

`F0000032` 在 document grain 的域分离 hash 证据如下（不记录原值）。Hash 输入严格为 UTF-8 字节串 `it-data:maintenance-spares:raw-enum:v1:S09:receipt_v1:F0000032`，随后追加单个 `0x00`，再追加 canonical UTF-8；算法为 SHA-256，bucket 使用 lowercase hex 的前 12 位并加 `hash_` 前缀。Canonicalization 为 Excel rich-text 顺序拼接、XML entity decode、UTF-8、Unicode NFKC、Unicode whitespace trim/collapse，不 casefold；canonical empty 记 NULL、不 hash：

| Hash bucket | 总计 | `010` | `011` |
|---|---:|---:|---:|
| `hash_020f4c198e68` | 5,790 | 13 | 5,777 |
| `hash_451224b2ffc1` | 3,778 | 3,777 | 1 |
| `hash_51d2fcc79d09` | 12 | 12 | 0 |
| `hash_7e5f048e25c5` | 212 | 212 | 0 |
| `hash_88908a54171a` | 385 | 385 | 0 |

校验：bucket 总计 10,177，`010` 总计 4,399，`011` 总计 5,778，缺失 0，同一 document 内 bucket 冲突 0。至少两个 raw bucket 同时跨越 `010/011`，再次证明不能按引用 presence 或字段优先级推断 `receipt_origin_kind`。

## 6. 自动质量门

1. ZIP/XLSX/XML 安全、隐藏 sheet、两行精确表头和内部 code 唯一检查。
2. header ID ↔ document no 双射；line ID 全 workbook 唯一。
3. 状态只接受版本化 approved raw→normalized mapping；unknown 失败关闭。
4. formula 只允许 exact code + position 的附件/报告列；不求值、不信 cached value。
5. optional empty merge 保持 NULL；identity empty merge 失败关闭。
6. typed projection 只保留业务必要字段和 digest，不保留整行 raw JSON。
7. `stable ID + canonical digest + mapping version` 幂等；修订只追加 version/supersedes。
8. candidate preview 必须 zero-write；正式 apply 必须重新解析相同字节并绑定 signed plan。
9. S07/S09 真实文件分别满足 wall ≤180 秒、peak RSS delta ≤768 MiB，240 秒硬超时。
10. source owner、正式 export view、as-of、revision/correction 和业务关系未锁定时，production apply 继续关闭。

## 7. 真实 parser 基准状态

- 解析器冻结文件证据：adapter SHA-256 `146df4dd95d7741fa072a44c453810b5d86048428fc080ce8a21c3302dad3665`；contract test SHA-256 `e9d8baeb5495d59f4a87d65b7aad09c854d4243ff3ea4e87dcafb6627ca1c748`。独立执行 240 项 focused test 全通过，`py_compile` 与 `git diff --check` 通过，六文件测试前后 SHA 不变。
- S07 在同一冻结文件 SHA 上完成全解析：19,572 个 document、69,298 条 line；146.673 秒，baseline/peak RSS 62,392/241,396 KiB，delta 179,004 KiB；outward `unknown_version`，数据库写入 0。1,142 条双上游引用冲突继续进入人工 ambiguity，没有按优先级猜选。
- S09 在同一冻结文件 SHA 上完成全解析：10,176 个有效 document、82,910 条有效 line（原始 82,911 行中 1 行缺 stable line ID）；157.602 秒，baseline/peak RSS 62,544/250,312 KiB，delta 187,768 KiB；outward `unknown_version`，数据库写入 0。剩余 38 条 ambiguity 为 PN 31、quantity 4、ParentIndex 1、缺 line ID 1、unknown version 1；受控字段造成的 2,484 条伪 `document_header` 冲突已归零。
- 两文件均满足 wall ≤180 秒、RSS delta ≤768 MiB 与 240 秒 hard timeout 门。该证据只计 `G1a-PARSER-SANDBOX` 技术验收，不计 source owner 批准、contract bundle Git freeze、production apply 或 authoritative source 证明。
- 历史诊断保留：更早一次 S09 运行在 86.499 秒以 `ambiguity_limit` 安全失败；该失败发生在上述修复之前，只证明旧快照会 fail-closed，不再代表当前冻结实现。

## 8. 当前判定

- `observed`：五份附件均完成。
- `candidate parser contract`：S07/S09；S08v2 optional；S06 待独立费用 preview 合同。
- `approved source contract`：0。
- `authoritative business source`：0。
- 生产写入：0。
- `G1a-PARSER-SANDBOX`：本地冻结文件、真实性能和独立代码复审通过；`.ai` candidate contract bundle 已由 Git commit `27dc6b842c67f190284af88398fef0941f834f1d` 跟踪，但 parser 代码仍无 commit/CI provenance。

**总体：完整五来源 authoritative 数据链仍不可合并；独立 G1a-P 代码切片已达到“可进入 commit/CI，仍不可生产”。**
