# PR 5A：氚云项目表字段合同

> 基线：`origin/main@caf4a973` | 依赖：PR 0 | Issue：`[MAINT-DATA-P0/question]`

## 1. 要解决的问题 (Problem)

当前系统没有氚云项目表的结构化字段合同。所有关于"氚云有哪些字段"的讨论都是口头猜测。在拿到脱敏真实样表之前，不能实现同步功能——猜字段会导致数据污染。

## 2. 达成的目的 (Goal)

- 取得至少一份脱敏氚云项目导出表（.xlsx）
- 用真实表头锁定字段契约：记录 ID、项目编号、名称、合同号、合同额、状态、维保起止日、回款计划日/金额、验收截止日、负责人原文、数据版本
- 写入 `docs/maintenance/import-field-contract.md` 和 `docs/maintenance/tritium-project-import.md`
- 明确来源所有权边界

## 3. 实现的路径

- [ ] 获取脱敏样表（由甲方提供，至少一份含项目变更的样例）
- [ ] 逐字段记录：表头原文、类型、必填性、唯一性、更新规则
- [ ] 写入文档 `tritium-project-import.md`：字段清单 + 来源所有权 + 同步策略
- [ ] 更新 `import-field-contract.md`：补充氚云专属章节
- [ ] 确认不可修改的记录 ID 字段
- [ ] 确认取消/重开/重复导出的业务规则

## 4. 验收标准

- [ ] 每个字段有真实表头、类型、必填、唯一性、更新规则
- [ ] 无稳定来源 ID → 结论是"人工核对"，不能按名称合并
- [ ] 来源所有权声明完整
- [ ] 脱敏 fixture 放入 `backend/tests/fixtures/`

## 5. 影响面

- 改动文件数：2 个 markdown + 1 fixture
- 不写任何代码
- 阻塞 PR 5B，不阻塞 PR 1-4
