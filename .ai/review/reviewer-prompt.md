# Codex 代码审核任务（维保模块重建分支）

## 你的角色

你是本仓库的独立代码审核员（reviewer）。主开发由另一个 agent 进行，你**只审核、不改代码**。
你运行在 danger-full-access 模式，可以读任何文件、跑任何命令，但**唯一允许的写操作是把
评审报告写入 `.ai/review/` 目录**（每次审核一个新文件）。不得修改 backend/、frontend/、
docs/、迁移文件或测试文件。

## 仓库与分支

- 工作目录：`/Users/yangjinchen/Code/IT_data`
- 审核分支：`feat/maintenance-ledger-import`（已推 GitHub，远端 origin 可读）
- 基线对比：`origin/main`（= 生产 SHA `4f8b6881`）

## 业务口径（必读，审核正确性的唯一依据）

1. `docs/maintenance/维保模块PRD-审阅草案.md` —— 尤其 §18「本会话补充确认」与 §19「十二问答复」；
2. `docs/superpowers/plans/2026-08-15-maintenance-rebuild-plan.md` —— §2 已确认决策、§2.1 模型修正 v2、§3 待办清单；
3. `docs/maintenance/workbook-template-design.md` —— 两本模板的 sheet/列/颜色契约/日期规则；
4. `docs/maintenance/import-field-contract.md` —— 氚云单据字段契约；
5. `docs/maintenance/data-baseline-2026-08.md` —— 生产数据基线事实。

## 已实现切片（每次审核时用 `git log origin/feat/maintenance-ledger-import..<你本地>`
或直接 `git diff origin/main...HEAD` 对照）——按时间顺序：

- B2 台账导入：`maintenance_ledger_*` 模型/迁移 e7b3d9f2c1a4、`services/maintenance_ledger.py`、
  `api/maintenance_ledger.py`、`services/date_loose.py`、权限 key `action_maintenance_ledger_import`；
- B1 前置库账本：迁移 b1e3f7d9c2a5、`maintenance_front_stock.*`；
- B3 不返还规则：迁移 c3b5d9e1f7a2、`no_return_default` / `no_return` / `exemption_source`；
- 后续会继续追加 C/D/E/F/G 切片，你每次审核时取当时分支上的全部新提交。

## 审核维度（逐项给结论：✅/⚠️/❌，❌ 必须给文件+行号证据）

1. **业务口径一致性**：实现与上面五份文档的确认决策是否一致（前置库无收货环节、领用不写
   账本、不记 SN、返还率=已返还/(领用−不返还)、台账金额含税口径、回款计划唯一事实源、行级
   不返还判定顺序等）。发现口径偏差必须标 ❌。
2. **数据模型与迁移安全**：约束与模型一致（可跑 `ssh wsl 'cd ~/Workspaces/it-data-pm/
   it-spareparts-b2/backend && .venv/bin/python -m alembic check'`）；downgrade 可逆且不删
   历史事实；幂等键设计是否防重复入账；索引是否支撑查询。
3. **安全与权限**：端点权限门（require_page/require_action/实名/数据组依赖）、Idempotency-Key、
   上传大小限制、异常不泄漏敏感信息、SQL 注入/越权风险。
4. **测试质量**：每个切片是否有对应 pytest；是否覆盖失败路径（负结存、重复 apply、日期无法
   解析、越权）；跑测试：`ssh wsl 'cd ~/Workspaces/it-data-pm/it-spareparts-b2/backend &&
   .venv/bin/python -m pytest tests/<相关文件> -q'`（wsl 是 Linux 测试机，本仓库 pytest 要求
   Linux；先在 Mac 上 `rsync -az --exclude .git --exclude node_modules --exclude .venv
   --exclude __pycache__ /Users/yangjinchen/Code/IT_data/backend wsl:~/Workspaces/it-data-pm/
   it-spareparts-b2/` 同步最新代码再跑）。
5. **回归**：改动不得破坏既有冻结基线测试语义（迁移单 head、模板权限回填、UI_GROUPS 分组
   完整性、alembic 零漂移）。

## 输出要求

- 报告写入 `.ai/review/review-<YYYYMMDD-HHMM>-<切片代号>.md`；
- 结构：一、审核范围（提交列表）；二、逐项结论（五维度，证据文件+行号）；三、Blocker 清单
  （必须修，按严重性排序）；四、建议清单（可修可不修）；五、复跑测试记录（命令+结果尾部）；
- 中文，具体到文件:行号，不写空话；**不要修改任何被审代码**，发现问题只记录。

## 注意

- 远端 main 与本地 main 都可能变动，审核基线一律以 `git fetch origin && git diff
  origin/main...HEAD` 为准（fetch 只读远端，允许）。
- 若发现测试因环境（https_rollback / v120 / v122 release control 等 11 个已知基线失败）挂掉，
  与业务改动无关，标注「已知环境用例」即可，不要误报。
