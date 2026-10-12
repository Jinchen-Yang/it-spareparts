# 按 PN 建议维保需求单（返还侧 + 领用侧）实施方案

- 日期：2026-10-12（上海）。
- 来源：一线同事界面标注（「登记返还」弹窗红箭头指维保需求单下拉与返件 PN）+
  上一轮诉求（领用单地址自动关联需求单，不再手填）。
- 状态：方案定稿，待多智能体核验后实施。
- 基线：`main` `d6a9802`（v1.36.5）。

## 0. 需求陈述（两句话）

1. **返还侧（同事标注）**：「登记返还」弹窗里选定返件 PN 后，维保需求单下拉应自动
   匹配出本项目下含该 PN 的活跃需求单作为置顶建议，用户仍可改选/清空/搜全量。
2. **领用侧（上轮诉求）**：「人工登记领用」表单里行内选定 PN 后，「关联需求单」
   从自由文本升级为按 PN 过滤的候选建议；当选定唯一需求单且有客户信息权限时，
   领用地址（site_location）给出**可编辑的**收货地址建议，免除手抄。

核心是同一个底层能力：**按项目 + part_id 查「含该备件的活跃维保需求单」**。

## 1. 与既有决策的边界（先立规矩）

- **D-26（0004-维保地址导入与旧模板兼容）规则 1**：禁止"根据需求单收货地址自动
  **覆盖**领用地址"。本方案只在**新建表单**上做**建议预填**：仅当 site_location
  为空且用户未编辑过时预填，标注来源，用户可改可清空，提交值仍以用户所见为准。
  绝不触碰导入/工作簿/已有单据的地址回写路径。
- **2026-09-11 拍板「不做逐件、逐 PN、逐条领用匹配」**：该口径约束的是**统计归集**
  （返还数量按项目/需求单汇总，不做 PN 级自动匹配）。本方案是**录入辅助**——
  建议候选仍由用户显式确认关联，统计口径、汇总恒等式（Σ需求单+未关联=项目总量）
  一律不变。
- **customer_info 脱敏**：`f_maintenance_order.receiver_address/receiver/receiver_phone`
  是脱敏字段。地址建议复用 `maintenance_order_contact.order_contact(order, user_ctx)`
  fail-closed 口径：无权限返回 null + `contact_info_state='restricted'`，前端不显示
  地址（也不显示 restricted 原文），绝不把脱敏数据搬进无脱敏字段落库。
- **服务端校验不变**：领用行 `demand_order_no` 仍走 `_resolve_demand_project`
  归属表校验；返还 `wbdd_no` 仍走 `_resolve_source_order_id`。建议只是帮用户填对，
  不是新的信任来源。

## 2. 后端改动

### 2.1 共享服务：demand_candidates 增加 part_id 过滤

`backend/app/services/maintenance_return_receipts.py:463` `demand_candidates(...)`
增加可选参数 `part_id: int | None`：

- 过滤口径（与现状叠加，不替换）：assignment（project_id + is_active）、
  `data_status='已生效'`、`active_beta_maintenance_orders` 墓碑口径、q 仍匹配
  order_no icontains；
- part_id 非空时用 **EXISTS 相关子查询**（不用 join）：
  `EXISTS (SELECT 1 FROM f_maintenance_line l WHERE l.order_id =
  f_maintenance_order.id AND l.is_active AND l.part_id = :part_id)`——
  免去重、count/分页天然正确，且能吃 `ix_ml_order_active` 索引；
- **排序维持现状不动**：`order_no asc, raw_order_id`（核验发现原方案把排序写成
  order_date desc 属笔误，实施时以现状为准，不做行为变更）；
- 响应行结构不变：`{source_order_id, order_no, order_date}`。

### 2.2 返还侧 API：加 Query 参数

`backend/app/api/maintenance_return_receipts.py:174` GET
`/maintenance/projects/stable/{project_id}/return-receipt-demands`
增加 `part_id: int | None = Query(None, ge=1)`。权限栈、路由、审计全部不动。
向后兼容：不传 part_id 行为与现在逐字节一致。

### 2.3 领用侧新端点：需求单候选（含脱敏地址）

`backend/app/api/maintenance_project_operations.py` 的
`site_issue_beta_router`（prefix `/maintenance/site-issues`）新增：

```
GET /maintenance/site-issues/projects/{project_id}/demand-candidates
    ?part_id=<int>&q=<str>&page=&page_size=
```

- 权限：`require_page("page_maintenance")` + `enforce_maintenance_project_access`
  （只读建议，不挂 action_maintenance_site_issue_manage——建议可见不等于可提交，
  提交链路的动作权限维持原样；与既有 return-receipt-demands 候选端点的权限栈
  同构）。注意该 router 注册时隐含 `require_maintenance_beta`（main.py:135/196），
  测试需 override `page_maintenance_beta`；
- 服务层复用 2.1 的 `demand_candidates`（带 part_id）。因该函数只回 dict 而
  `order_contact(order, user_ctx)` 需要 `FMaintenanceOrder` 实体，端点层按
  source_order_id（=raw_order_id）补一次实体查询，再逐行附加：
  `receiver_address / receiver / receiver_phone / contact_info_state`；
  restricted 时三个字段为 null、state='restricted'；
- `record_access_log(ctx, "site_issue_demand_candidates", "maintenance_project",
  {project_id, part_id, searched, total})`，与既有候选搜索审计惯例对齐；
- 响应：`{rows: [{source_order_id, order_no, order_date, receiver_address,
  receiver, receiver_phone, contact_info_state}], total, page, page_size}`。

### 2.4 明确不做

- 不加 DB 迁移（纯只读接口 + 前端）；alembic head 保持 `e7c1f9a3b5d2`。
- 不改 Excel/工作簿导入、导出、签名基线、并发保护。
- 不改统计口径与汇总恒等式。
- 不做 PN 强制校验（返件 PN 本就不限制为原领用 PN，2026-09-11 §2.1）。

## 3. 前端改动

### 3.1 API 客户端（frontend/src/api/maintenanceOperations.ts）

- `getReturnReceiptDemands` 增加 `part_id?: number` 透传；
- 新增 `getSiteIssueDemandCandidates(projectId, {partId?, q?, page?, pageSize?})`
  → GET 2.3 端点，返回类型含地址与 contact_info_state。

### 3.2 功能 A：登记返还（ReturnReceiptsSection.tsx）

- PartPicker `onChange`（f:769-781）拿到 partId 后，调
  `getReturnReceiptDemands({part_id, page_size: 50})` 存 `pnMatchedDemands`；
- 需求单 Select（f:754-766）改分组 options：`含此 PN`（置顶，label 带 order_date）
  与 `其他需求单`（已加载全量里扣除匹配单）；未选 PN 时维持现状单列表；
- 匹配唯一且 wbdd_no 当前为空 → 预选该单（可 allowClear 清空）；用户已选则绝不
  覆盖；匹配为空 → 在下拉里显示一条禁用项「本项目无含此 PN 的需求单」提示；
- PN 改选/清空时只更新建议分组，不撤销用户已选的 wbdd_no（PN 与需求单无强制
  绑定，属既有口径）。

### 3.3 功能 B：人工登记领用（ManualSiteIssueSection.tsx）

- 行内「关联需求单」（f:709-719）自由文本 Input → AutoComplete（可输可选可清空）：
  行内已选 partId 时按 part_id 拉候选；未选 partId 时提示先选 PN（不拉全量，
  避免无界下拉）；手输不在候选内仍允许提交（服务端校验兜底 400 文案不变）；
- 编辑既有单回填的 demandOrderNo 原样显示；
- 地址建议：某行选定需求单后，取该单候选行里的 receiver_address（state 非
  restricted 且非空）；仅当 site_location 为空且用户未手动编辑过时预填，并在
  字段下方标注「已按需求单收货地址预填，可修改」；多行选定不同需求单地址不同、
  或地址为空、或 restricted → 不预填、不加标注；用户一旦手动编辑过 site_location，
  本会话内不再自动预填。两组件均无现成 dirty 追踪，**需自补 useRef 标记**
  （onChange 置 dirty，打开新表单/编辑回填时重置）。restricted 时在地址字段下方
  显示中性提示「无客户信息权限，地址建议不可用」，不显示 restricted 原文。

## 3.4 决策文档补记（核验必改项）

`docs/decisions/0004-维保地址导入与旧模板兼容.md` 追加 dated 补记：页面新建
领用表单上的「按需求单收货地址建议预填」属于显式 UI 建议（空值且未编辑时预填、
可见可改可清空、标注来源），不构成规则 1 禁止的「自动覆盖」，与配套 sync-plan
中「不自动互相代填」的口径以「用户可见并可修改的建议」为界。发布时同时提示
管理员：领用登记人若需地址建议，须授予 `data_customer`（客户信息）权限——
典型维保负责人/仓库角色模板当前无此权限，功能 B 的地址部分对其默认不生效
（按 PN 关联需求单的候选建议不受影响）。

## 4. 测试计划

### 后端（pytest，本地 5433）

1. `tests/test_maintenance_return_receipts_api.py` 扩展：
   - part_id 命中：有该 PN 行的单返回、无该 PN 行的单排除；
   - 同单两行同 PN 只出现一次；
   - line is_active=false 不命中；
   - 不传 part_id 与现状一致（回归快照对比行集）；
   - part_id + q 叠加。
2. 新 `tests/test_site_issue_demand_candidates_api.py`：
   - 只返回本项目活跃需求单（跨项目单排除）；
   - 有 customer 权限（data_customer）→ 地址可见；
   - 无权限 → 三字段 null + state='restricted'；
   - 未生效/墓碑/assignment 失活排除；
   - 审计日志落库（sys_access_log action 校验）。

### 前端（vitest）

3. `ReturnReceiptsSection.test.tsx`：选 PN 后出现「含此 PN」分组；唯一匹配且
   wbdd_no 空 → 预选；已有选择不覆盖；无匹配提示。
4. `ManualSiteIssueSection.test.tsx`：选 PN 后关联需求单出现候选；选定后空地址
   被预填+标注；手动编辑过地址不再预填；restricted 不显示地址。

### 实机（本地栈 8000/5176）

5. 浏览器全链路点按：登录 → 维保页 → 登记返还（选 PN 看分组/预选）→ 人工登记
   领用（选 PN 看候选/地址预填）→ 提交成功 → 列表回显。

## 5. 发布计划（无迁移最短路径）

1. 分支 `feat/pn-demand-suggestion`（自 main d6a9802）→ PR → CI 两 job 全绿；
2. @Jinchen-Yang 已于 2026-10-12 一次性授权合并与上线：精确 head SHA 审批后
   `gh pr merge`（必要时 --admin）；
3. 生产：先 `backup.sh` 全量备份+SHA256 校验；旧镜像打 rollback tag；
   按 v1.36.4 模式仅替换 app/frontend 两容器；`alembic upgrade head && alembic check`
   确认 head 仍为 `e7c1f9a3b5d2`；健康检查+冒烟；失败即切回旧镜像（回滚预案）；
4. 回填发布记录文档，打 tag。

## 6. 验收标准（对齐需求陈述）

1. 返还弹窗选 PN 后，需求单下拉出现「含此 PN」置顶建议，可改选可清空，不选 PN
   行为与现状一致；
2. 领用表单选 PN 后，行内关联需求单出现候选建议，仍可手输/留空，服务端校验不变；
3. 有权限用户在领用表单选定需求单时空地址自动预填并可修改，无权限用户看不到
   任何地址；手动编辑过的地址永不被覆盖（D-26 规则 1 不破）；
4. 统计口径、汇总恒等式、导入导出、审计与权限模型全部不变；
5. 后端/前端测试全绿，CI 全绿，实机冒烟通过。
