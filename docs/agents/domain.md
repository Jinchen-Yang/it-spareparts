# Domain Docs 路由

本仓库采用 single-context：根目录 `CONTEXT.md` 定义全局业务语言，`docs/adr/` 记录架构决策，模块文档和 `.ai/contracts/` 提供更窄的正式口径。

## 1. 阅读顺序

1. `AGENTS.md`：开发边界、数据链路、验证与发布规则。
2. `CONTEXT.md`：系统主干、统一术语和业务对象。
3. 与任务直接相关的 ADR、模块文档和契约。
4. 当前源码、Alembic 迁移、测试与可用的数据库只读事实。

事实冲突时遵循 `AGENTS.md` 中的优先级；旧计划、归档文档和注释不能覆盖当前数据库或源码事实。

## 2. 按任务路由

| 任务主题 | 优先读取 |
| --- | --- |
| 维保业务与页面 | `docs/maintenance/ARCHITECTURE.md`、`docs/maintenance/REQUIREMENTS.md` |
| 维保 Excel / 工作簿 / 字段契约 | `docs/maintenance/import-field-contract.md`、`docs/maintenance/contracts/`、`.ai/contracts/maintenance-spares/` |
| 回款提醒 | `.ai/contracts/maintenance-collections/` |
| 数据质量 | `docs/data-quality/`、`docs/adr/0001-separate-row-quality-issues.md` |
| 互通池与策略覆盖 | `docs/pool-analysis/`、`docs/pool-policy-coverage/` |
| 价格纪律 | `docs/price-discipline/` |
| 发布、迁移与回滚 | `docs/DEPLOY.md`、`docs/releases/`、当前 Alembic 链 |
| 多 Agent 认领与交接 | `.ai/AI_WORKFLOW.md`、`docs/agents/issue-tracker.md`、`docs/agents/collaboration.md` |

只读与任务直接相关的最小文档集合，不要为了“建立上下文”全量扫描归档目录或 `.ai/CHANGELOG.md`。

## 3. 使用规则

- Issue 标题、测试名和实现说明使用 `CONTEXT.md` 的统一术语。
- 若实现会违背 ADR，必须在计划和 PR 中显式指出，不得悄悄覆盖。
- 若正式文档与当前运行事实不一致，记录差异和证据，再由 Issue/ADR 决定修正文档还是实现。
- 新的长期决策写 ADR；单项任务的进度和阻塞写 Issue/PR，不写进 `CONTEXT.md`。
- 归档文档只用于追溯，除非当前正式文档明确引用，否则不能直接作为新实现依据。
