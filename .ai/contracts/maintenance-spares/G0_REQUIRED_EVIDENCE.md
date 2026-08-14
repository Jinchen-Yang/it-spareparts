# G0 Required Evidence — Fast Resume Checklist

本清单只收“外部真实来源证据”，不再向用户索要产品选择。文件可原样放入受控本地目录或作为附件提供；原始 Excel 不进入 Git，扫描结果只登记 source ID、header signature、状态聚合和 SHA-256。

## 1. S02 采购与 S03 销售覆盖证明

每个来源由 source owner 提供或确认：

- 正式 export view 名称与 owner；
- 全量或增量导出；
- `coverage_from`、`coverage_to`、`source_as_of`、`latest_complete_business_date`；
- 导出频率、freshness SLA、断批/漏批判断方式；
- S02 采购类型字段与 inclusion/exclusion allowlist。

只有两个来源都完整覆盖 `[D-90,D)`，系统才能把“零命中”解释为真实无采购销售；否则只返回证据不可用。

## 2. S04 权威库存/仓库关系

需要一份正式库存快照及仓库主数据/关系导出，至少包含：

- 产品库存 stable ID、part stable ID、数量；
- warehouse stable ID，不只仓库名称；
- warehouse type：公司主库或地区库；
- 项目 stable ID → 所属地区 stable ID；
- source as-of、可用量与在库/预留/安全库存的口径。

缺任一稳定关系时不计算 `company_available` 或 `assigned_region_available`。

## 3. S07 正式发货/出库合同剩余证据

真实只读 Excel、SHA、candidate 双表头 signature、header/line stable ID 和样本内状态聚合已经收到。仍须由正式 source owner 确认：

- 正式 export view 与已收到 candidate 是否同一来源；
- WBDD/source stable reference、warehouse stable ID、part/SN 规则；
- 实际观察到的原始中文状态值、其业务含义，以及正式批准的 raw→normalized `pending/confirmed/void` 映射；
- 修正是否复用 stable ID，revision/correction/supersedes 如何识别；
- source owner 与导出日期/as-of。

发货 confirmed 只产生在途 delivery source，不等于现场收货。

## 4. S09 正式入库合同剩余证据

真实只读 Excel、SHA、candidate 双表头 signature、header/line stable ID、仓库/库位字段和样本内状态聚合已经收到。仍须由正式 source owner 确认：

- 正式 export view 与已收到 candidate 是否同一来源；
- warehouse/location stable ID、part/SN 规则；
- 实际观察到的原始中文状态值、其业务含义，以及正式批准的 raw→normalized `pending/confirmed/void` 映射；
- 修正 revision/supersedes 识别；
- 检测结果何时构成可用正式入库；
- 入库来源类型如何稳定识别；`退返入库通知单`、`关联退返通知单`、`采购单号` 必须分别保留，禁止 fallback 压成一个引用；只有获批的 maintenance/good-return inbound 类型与 exact return relation 才能参与好件返还分配；
- source owner 与导出日期/as-of。

好件 return line 只在 S09 confirmed current line 完成逐行/部分分配后闭环。

## 5. S06 费用数据源剩余证据（post-M1，不阻塞备件 M1）

真实只读 Excel、SHA、43 列双表头、header/line stable ID 和结构聚合已经收到。正式费用 apply 前仍须由财务/source owner 确认：

- 正式 export view、source owner、导出 as-of；
- “记录ID(不可修改)”是否为跨导出稳定 ID，以及 revision/correction/supersedes 语义；
- 哪些原始流程状态才计入项目成本，作废/退款/冲正如何表达；
- 唯一正式计入的行级金额、正负号、币种、税基/税率、含税/未税口径；
- 费用发生日期、报销日期、支付日期分别来自哪个字段；
- 销售订单 stable ID 到项目/合同 stable ID 的 exactly-one 关系。

证据未齐前只能运行无 Session、无 archive/batch/audit 的 pure preview；禁止调用通用 `/api/import/upload`，禁止写 `f_project_expense`，禁止按销售单/项目删除旧费用后重插。

## 6. 不阻塞项

- S08 外部退货返库为 V1 optional 对账来源；native 好件/坏件返还流程不依赖它。
- opening balance 不进入 V1；首批试点只接受实名盘点为零的前置库。
- S06 正式费用 apply、维修/报废/变卖、虚拟采购销售、销售驾驶舱不属于首个备件闭环；S06 pure preview 可独立并行。

## 7. 收到证据后的自动恢复顺序

1. 只读复制到 repo 外 0700 临时目录，计算 SHA，不打印业务行；
2. 更新 source registry/state mapping/机器可读 manifest，并运行 parser sandbox contract tests；
3. 两名独立 Reviewer 先通过 G1a-PARSER-SANDBOX；完整 G0 通过后再审 G1a-RELATION/DELIVERY；
4. 只有完整 G0 与 G1a-RELATION/DELIVERY 通过后，才恢复 Claude Code 的 DeepSeek v4flash 协调器执行 G2 与 G1b；
5. 从 reviewed `KERNEL_SHA` 同时启动 Lane A/B/C；
6. 集成、全量测试、独立审查、CI；
7. 生产副本演练、DB+uploads 全量备份与隔离恢复；
8. 全 flag=false 部署、命名灰度、0/5/15/30 分钟观察和次日对账。
