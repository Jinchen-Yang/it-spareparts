# 维保业务文档

> 维保 = **后事实数据展示板**：让老板看清每个维保项目的「货」和「钱」（重点是项目成本与备件申请）。

## 两本活文档（业务理解的唯一入口，实时更新）

1. **[ARCHITECTURE.md](./ARCHITECTURE.md)** — 维保业务架构总文档：定位、角色权限、事实源分离、成本口径、归属粒度、上传节奏、页面结构、冻结清单。
2. **[REQUIREMENTS.md](./REQUIREMENTS.md)** — 核心需求增量表：每条已确认口径的日期、内容、状态、出处；改判不删旧行。

## 执行与实现文档

- [plan-v1.3-fullstack.md](./plan-v1.3-fullstack.md) — v1.3 全栈执行计划（当前执行版本）。
- [import-field-contract.md](./import-field-contract.md) — 氚云四类单据＋台账的字段契约（稳定参考）。
- [workbook-template-design.md](./workbook-template-design.md) — 工作簿模板设计（报销/回款 04/05 sheet 的依据）。
- [云端执行环境说明.md](./云端执行环境说明.md) — 云端执行环境接入说明（ops）。
- [templates/](./templates/) — 台账/项目工作簿模板 xlsx。

## 发布文档

- [../releases/v1.23-M0-confirmation-checklist.md](../releases/v1.23-M0-confirmation-checklist.md) — M0 追认签署清单（2026-08-16 全签）。
- [../releases/v1.23-addon-pack.md](../releases/v1.23-addon-pack.md) — 增补包 AB-1~4（权限改判/需关注/报销回款/购物车解冻）。
- [../releases/v1.23-migration-audit.md](../releases/v1.23-migration-audit.md) — 迁移链逐修订审计。

## 历史归档

- [archive/](./archive/README.md) — 已归档文档时间线（旧 README、甲方核心需求、业务手册、dev12–16、v1.1/v1.2 计划等）。
