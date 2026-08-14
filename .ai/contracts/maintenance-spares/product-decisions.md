# Product Decisions — Maintenance Spares V1

记录时间：2026-08-13（Asia/Shanghai）

决策责任：Root Product/Release Owner
授权依据：用户明确要求由本 Agent 全权承担产品经理与发布责任、减少用户额外抉择；真实数据合同仍不得由产品判断替代。

## 已冻结的产品决策

1. 申请首先检查公司主库和项目所属地区库；足量产生 `InternalAllocationIntent`，不足才进入补库采购审核。其他项目前置库只做管理证据，不做日常可调库存。
2. 规则窗口固定为提交业务日 `D` 前 90 个完整自然日 `[D-90, D)`；`D` 取实名提交时间在 `Asia/Shanghai` 的日期，采购/销售业务日分别取 `order_date`。
3. 采购/销售有效状态只认原值 `已生效`；未知状态失败关闭。有效行还必须有稳定 part ID 和正数量。
4. 采购事实只接受 G0 冻结的采购类型 inclusion allowlist；`采购申请` 排除，未知/新增类型失败关闭并人工复核。禁止“除已知排除项外全部计入”的开放式 fallback。价格证据另按税务/成本规则筛选，不与存在性混为一谈。
5. 只有 S02/S03 对完整 `[D-90,D)` 都证明 `coverage_state=complete` 时，若窗口内采购和销售均无有效事实，系统才硬驳回 `NO_VALID_PURCHASE_AND_SALES_IN_90D`。任一来源 partial/stale/unavailable/missing batch 时只报证据不可用，不开放人工覆盖或新品例外。
6. 正常申请先过确定性规则，再由不同于提交人的实名审核人逐行批准；系统硬拦截不可覆盖。warning 允许审核但必须写逐行理由。V1 不使用 LLM 自动批准/驳回。
7. 审批结果只产生 `InternalAllocationIntent` 或 `ApprovedPurchaseIntent(waiting_procurement_execution)`；库存影响和对外法律效力均为 none。WBDD/采购顺序锁定前不自动导出 WBDD。
8. 现场收货采用 IT_data 原生实名表单；发货 confirmed 只表示在途，不表示现场已收。收货不足只显示 remaining receivable，不冒充已登记差异。
9. 每个命名试点项目只允许一个 active 前置库；由 Admin 版本化 provisioning，不按项目名 seed。
10. 首批申请人仅限已有项目责任关系或受控 ApplicantGrant 的实名账号；销售关系没有稳定来源前不开放“销售本人全部项目”。
11. 现场领用 V1 支持同 PN replacement/new install；跨 PN replacement 失败关闭。客户端不能决定返还豁免。
12. 默认坏件应返；只有领用确认时已生效且有证据的项目硬盘免返政策可豁免拆下坏硬盘。未用新硬盘始终走好件退回。
13. 坏件仓库实收只表示返还义务履行并进入 pending inspection，不恢复公司可用库存。好件必须按 S09 正式入库行分配完成后才闭环。
14. 领用冻结的是 provisional cost evidence，不等于财务确认项目实际成本；无权限账号显示 masked，不显示 0。
15. V1 试点项目必须有实名零余额盘点证明；非零余额项目不进入首批试点。opening balance 等 S12 盘点来源合同建立后再做，不得伪造 receipt/delivery。

## 仍由真实数据决定、产品无权猜测

- S04 稳定仓库 ID、公司/地区类型、项目所属地区关系和真实 as-of。
- Required S07/S09 的正式 export view、完整双表头、状态原值与语义、稳定 ID 和样例 SHA；S08 为 V1 optional 外部返还对账，未来接入时执行同一合同门禁。
- Required S07/S09 修正是否复用 header/line stable ID，以及可审计 source revision/supersedes 证据；S08 接入时同样证明。
- 哪些品类强制逐件 SN。
- S09 检测结果何时构成“可用正式入库”，以及入库来源类型如何稳定区分维保返还入库与采购等其他入库；三个上游引用必须独立保存，不用 fallback 猜测。

上述任一缺失都使 G0 保持失败关闭。
