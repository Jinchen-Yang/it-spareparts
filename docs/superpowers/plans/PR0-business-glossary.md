# PR 0：固定业务口径与 Git 状态

> 基线：`origin/main@caf4a973` | 依赖：无 | 可并行：是（与 PR 1 无依赖冲突）

## 1. 要解决的问题 (Problem)

当前 `docs/maintenance/business-handbook.md` 写在 v1.20 时期，使用术语"项目经理"，且业务描述暗示 IT_data 是维保项目的日常新建入口。实际情况是：

- 维保项目在氚云建立，IT_data 负责同步和履约跟进，不负责业务立项；
- "项目经理"和"维保人员"是同一类业务角色，统一称为"维保负责人"；
- Issue #128, #136, #193 仍然描述着旧口径，需要更新为 #244 后的真实状态；
- 8 个 Draft PR（#194, #202, #211-#216, #243）的内容已由 #244 整合进 main，但这些 PR 仍开着，误导后续开发。

## 2. 达成的目的 (Goal)

- 三份维护文档口径一致：`business-handbook.md`, `import-field-contract.md`, `README.md`
- 所有文档中的"项目经理"→"维保负责人"
- 所有文档不再描述"IT_data 日常新建维保项目"
- Issue #128, #136, #193 引用 #244 为已完成项，描述本计划的后续依赖
- 8 个被取代 Draft PR 评论 `superseded by #244` 后关闭
- 新建 6 个窄 Issue，依赖关系写入 #128

**本次不改：**
- 不创建任何代码文件
- 不修改 `CLAUDE.md`
- 不提交

## 3. 实现的路径 (Implementation Plan)

- [ ] **Step 1**: 更新 `docs/maintenance/business-handbook.md`
  - 全局 s/项目经理/维保负责人/g
  - 更新 5.1 节：标注 #244 已合并，列出 main 中实际已有的能力
  - 更新 5.2 节：移除"尚未发布"措辞（#201, #203-#210 已在 main）
  - 更新 6.0 节：标记已在 #244 合入的差距项为已完成
  - 更新元数据：最近核对日期 → 2026-08-12

- [ ] **Step 2**: 更新 `docs/maintenance/import-field-contract.md`
  - s/项目经理/维保负责人/g
  - 补充氚云字段预留段落（来源 ID、项目编号、合同号等，标注"待真实样表确认"）

- [ ] **Step 3**: 更新 `docs/maintenance/README.md`
  - 补充本计划 (#128) 和 PR 依赖图的入口链接
  - 标注 `business-handbook.md` 为权威口径

- [ ] **Step 4**: 处理被取代的 GitHub Draft PR
  - 对 #194, #202, #211-#216, #243 逐项评论：`Superseded by #244. Closing.`
  - 关闭这些 PR
  - 不在本地删除对应分支

- [ ] **Step 5**: 更新 Issue #128, #136, #193
  - #128：添加 #244 合入状态说明 + 本计划 7 个 PR 的依赖图
  - #136：B1 改为"项目由氚云建立，IT_data 负责同步"
  - #193：统一术语为"维保负责人"，删除"IT_data 日常新建项目"描述
  - #201, #203-#210：标注已完成，#209 另开仓库样表 Issue

- [ ] **Step 6**: 新建 6 个开发 Issue
  - `[MAINT-UX-P0]` 可折叠菜单与正式路由
  - `[MAINT-SEC-P0]` 维保需求单按项目负责人隔离
  - `[MAINT-UX-P0]` 我的待办与项目详情业务化
  - `[MAINT-DATA-P0/question]` 氚云字段合同
  - `[MAINT-LINK-P0]` 项目采购链展示
  - `[MAINT-DATA-P1/question]` 仓库真实模板接入
  - 每个 Issue 引用本计划文档，添加对应 PR 标签

- [ ] **Step 7**: 提交
  - Commit message: `docs: freeze maintenance business handbook and issue state (#128)`
  - 不创建 PR（等 PR 1-3 完成后一起提交或独立 PR）

## 4. 验收标准 (Acceptance Criteria)

- [ ] 三份文档不再出现"项目经理"（统一为"维保负责人"）
- [ ] 三份文档不再出现"IT_data 日常新建维保项目"的业务描述
- [ ] 8 个被取代 PR 已关闭
- [ ] 6 个新 Issue 已创建，依赖关系已在 #128 中注明
- [ ] `business-handbook.md` 5.1 节正确反映 main@caf4a973 的实际能力

## 5. 影响面与风险 (Impact & Risk)

- **改动文件数**：3 个 markdown 文件
- **是否架构变动**：否
- **是否破坏已有接口**：否
- **是否需要数据迁移**：否
- **已知风险**：无。这是纯文档 + Issue 操作，不动任何代码。
