# ITSP-6 第一个候选：字段级人工保护

决策: D-34

基线 `319e25f6cd2b2c0119a0e865893a38a6cf9ca362`，分支 `feat/field-level-manual-protect`。
只实现 D-34 第 1、2 款；取消状态及连带作废留给下一候选。不触及生产，不推送或开 PR。
指定必读 `docs/agents/workflow.md` 在此基线不存在；已读取 AGENTS、现存 agents 文档、CONTEXT、D-27/31/33/34、领用原语契约及指定技能。

## 勘察证据（基线行号）

人工修改 → 记录保护 → 原件 upsert 的链路：

1. 网页 `PATCH /maintenance/demands/lines/{raw_line_id}` 完成实名、动作权、项目范围及 digest 校验，再调用服务，同事务提交或回滚（`backend/app/api/maintenance_demands.py:457`）。
2. `maintenance_demand_manual.patch_demand_line` 持全局数据锁、项目状态、单头、明细锁；归一化后比较实际值，写 `manual_override` 的首次原值、人工值、操作者和时间，不改变来源行的 `edited_source`（`backend/app/services/maintenance_demand_manual.py:232`、`:284`、`:372`）。白名单缺少备注 `line_note`（`:58`）。
3. 项目总表 V2 03 的归一化差异形成 cost_refills，apply 在既有整本锁链和事务内更新事实、人工价和审计；但所有 UPDATE 均执行 `edited_source = workbook_manual`（`backend/app/services/maintenance_project_master_workbook.py:6847`、`:6898`），导致备注/人工价冻结数量。
4. loader 的 `_upsert_maintenance_lines` 用 SQL CASE 保留账本字段或非 wbdd 行，再独立用 Python 重算有效差异和审计（`backend/app/etl/loader.py:382`、`:455`、`:483`、`:508`）。两处重复判定；PN 只联动 part_id，备注不在保护集合。
5. `manual_override` 已是 JSONB 字段；人工成本独立存于 `MaintenanceManualCostOverride`。因此无需 schema 变更；新增逻辑复用两种现有证据，不把人工成本混入原件事实账本。
6. 已有撤销 `POST .../clear-override`（`backend/app/api/maintenance_demands.py:543`），还原首次人工修改前的 `source_value`（`backend/app/services/maintenance_demand_manual.py:588`），不是最新上传值。采用新原件需要重新上传。
7. 人工新建依据稳定原始 ID 前缀识别（`backend/app/services/maintenance_wbdd_reconciliation.py:64`），不能仅凭 edited_source 把人工改过的来源行当人工新建行。
8. 领用事实由 `maintenance_site_issue_facts.apply_locked_site_issue_edits` 按独立单/行版本、审计和原语协议处理。需求字段保护只作用于 WBDD 需求，不同步修改真实领用/收货；06 原语及锁序保持。
9. V1 `apply` 的备注分支也直接写 `line_note`（总表服务 `:1299`）。当前 V1／全局 Excel 模板没有需求备注列，该分支是公开计划服务的兼容能力；本次接入保护但不新增模板列。

## 三路结论与实现选择

- 用户场景：有权业务人员改备注/补价后继续重传 WBDD。回执必须区分实际变更、无变化和被挡字段；沿用现有上传统计和前端展示。
- 业务边界：新来源行的修改保持 wbdd 来源，只写实际变更字段账本。PN 文本与 part_id 为身份组。历史非 wbdd 行继续整行保护，后续人工编辑不构成解除历史保护的证据；人工新建不参与待核对来源清单。
- 技术边界：共用新模块提供归一化差异、账本记录、Python/SQL 保护判定。工作簿只接线需求 UPDATE，不修改 01 项目概览。网页补备注和只读列表；不新增 ITSP-5 工作台。
- 人工价沿用已有人工成本表及价格版本/摘要；数量不被锁住；身份改变仍使旧 PN 人工价失效。
- 不增加迁移，不转换任何历史行，不清空人工账本。历史整行保护不因字段账本新增而降级。

## 公共契约与兼容

`maintenance_field_protection` 统一维护事实白名单、数量三位精度及文本归一、真实差异、PN 身份组、首次快照账本、Python／SQL 判定和人工新建前缀。入口继续拥有验证、锁、审计、提交和成本规则；模块不自行加锁或提交。`manual_override[field].source` 记录 `page_manual/workbook_manual`，但不修改来源行的 `edited_source`。

- `PATCH /api/maintenance/demands/lines/{raw_line_id}` 既有接口增加 `line_note`。读明细、修改及撤销回执返回 `protected_fields`，未来网页编辑可直接复用。
- `GET /api/maintenance/demands/manual-protection-review?page=1&page_size=50` 返回 `items/total/page/page_size`，每页上限 200。沿用维保页、需求管理动作及项目范围权限；列表和总数同一筛选，排除人工新建、无效行和墓碑。不给出成本、人工账本原值或转换动作。
- 既有 clear-override 恢复首次人工修改前快照；PN 同时恢复及解除三字段账本。回执包含 `source_reupload_required` 和中文说明。历史整行保护或人工新建保护仍在时明确说明，不承诺重传能越过保护。
- 原件回执沿用 `fact_rows_semantic_updated/fact_rows_unchanged/fact_rows_protected/protected_field_counts/fact_rows_legacy_review`。实际变更与人工保护可以落在同一行；同键匹配数不是实际覆盖数。上传的历史待核对数仅指本批实际被挡行，完整清单用上述 API。
- 合法原件最终改变 `part_id` 时，同事务停用旧 PN 人工价并留审计；被保护挡住的 PN 或同批最终恢复原身份不退役。人工价保持既有自动成本优先的瀑布，不新增价格优先级。
- 锁顺序保持全局数据锁→项目状态→单头→归属→明细→下游价格／领用。网页锁内新鲜重读行与项目状态，旧 digest 返回 409；保护账本、业务值、审计和 revision 同事务。
- 需求数量与退货数量按既有数据库三位精度处理，金额仍两位；只改备注时不会把未改的 `2.001` 截成 `2.000`。

本次未修改前端。已有原件上传回执组件已能展示备注和 PN 保护字段；网页增加备注输入控件等 ITSP-5 工作台工作留给后续。

## 测试 seam 与验收场景

依据任务指定的公共 seam 及开发技能授权，自主确定以下测试边界，无需再确认：网页 API/公开编辑服务、工作簿 build/validate/apply、loader.load 和公共保护服务。真实 PostgreSQL 验证事务及并发。

共同起点：独立测试库、有项目归属的有效来源需求行、有权实名人。每轮先失败再实现。

| 场景 | 操作 | 预期 |
|---|---|---|
| 备注 | 网页/总表仅改备注，upsert 改数量和备注 | 数量更新，备注保留，回执列 line_note |
| 人工价 | 仅补人工未税成本及原因，再 upsert 数量 | 数量更新，人工成本证据保留 |
| PN | 改 PN，再上传旧 PN 和新数量 | pn_raw/pn_std/part_id 同组保护，数量更新 |
| 原样回传 | 归一化等值整行回传，再 upsert | 无新增保护/审计/版本，原件可覆盖 |
| 历史保护 | 非 wbdd 来源行无完整字段证据，再 upsert | 继续整行保护，出现在范围受控待核对清单 |
| 人工新建 | 网页/总表新建后同键导入 | 继续保护，不误列为待核对来源行 |
| 一致性 | 两入口提交同字段变化 | 同样字段集合及 loader 判定 |
| 冲突/原子性 | 旧 digest/旧 Excel 或真实并发 | 409/既有工作簿冲突，无半写；保留现有锁测试 |
| 撤销 | 人工修改后原件被挡，再撤销 | 恢复首次快照，明确要求重传以采用新原件 |

## 待金臣确认

目前无新增业务口径需要确认。D-34 第 3–7 款明确不在本候选实施。

## 验证与交付记录

本机默认 `:5433` 首轮红测已复现：网页拒绝备注、总表备注冻结数量、原件覆盖人工备注。后续使用本任务独立临时 PostgreSQL 15 容器 `itsp-laneB-fieldprot-20261020`、本机 `:32872`；每次 pytest 仍由 conftest 新建独立库，全部合成夹具。

中断后按用户资源约束串行运行测试，无 xdist。上轮未结束的日志不作为通过证据。数量精度补充红测确认只改备注把 `2.001` 改成 `2.000`；随后仅调整需求数量解析，保留金额精度。

独立静态审查与编码分工由不同代理完成（同模型角色，不声称异构审查）。审查提出的 PN 旧价退役、PN 撤销残留、锁内旧 ORM 及三位数量精度均纳入契约验证。

新增 3 个契约测试文件，共 30 个参数展开后的用例：API 6 个、loader 14 个、工作簿 10 个。既有测试不删除；`test_demand_manual_edit.py` 只纠正“网页编辑会升格来源”的过时注释。相关子集命令如下，结果 **303 passed / 0 failed，250.38 秒**：

```bash
cd backend
export PYTEST_DATABASE_BASE_URL=postgresql+psycopg://spareparts:spareparts@127.0.0.1:32872/spareparts_test
uv run --extra dev pytest \
  tests/test_maintenance_field_protection_api.py \
  tests/test_maintenance_field_protection_loader.py \
  tests/test_maintenance_field_protection_workbook.py \
  tests/test_demand_manual_edit.py \
  tests/test_demand_manual_p1_guards.py \
  tests/test_demand_manual_api_guards.py \
  tests/test_wbdd_upload_feedback.py \
  tests/test_maintenance_project_master_v2_editable.py \
  tests/test_maintenance_master_v2_lock_freshness.py \
  tests/test_maintenance_project_master_workbook.py \
  tests/test_maintenance_site_issue_facts.py \
  tests/test_maintenance_site_issue_facts_compatibility.py \
  tests/test_maintenance_site_issue_facts_errors.py -q --tb=short
```

后端全量命令：同目录、同测试数据库环境变量，`uv run --extra dev pytest`。只运行一轮，无 xdist：**5978 passed / 1 failed / 7 skipped，5855.26 秒（1:37:35）**。唯一失败是既有 `test_edge_release_control.py::test_issue153_runbooks_are_clean_shell_copyable_and_define_inputs` 调用 `shellcheck` 时发生 `FileNotFoundError`，开发环境缺少工具；不是断言失败，也未修改相关源码或测试。

仅在本工作树的 `.venv` 中执行 `uv pip install --python .venv/bin/python shellcheck-py`，得到 ShellCheck 0.11.0（包版本 0.11.0.1），未修改项目依赖清单或系统环境。全量退出后串行补跑：

```bash
uv run --extra dev pytest \
  tests/test_edge_release_control.py::test_issue153_runbooks_are_clean_shell_copyable_and_define_inputs \
  -q --tb=short
```

补跑结果 **1 passed / 0 failed，7.62 秒**。两轮结果合计验证全部 **5979 个启用用例通过**，无未解决失败；7 个原有跳过项仍保留。遵守用户全量只跑一轮的资源限制，没有重新运行整套。全量包含本次 30 个新增契约以及原有总表、loader、领用原语、迁移升级／降级和 `alembic check` 无漂移回归。未修改前端，不运行前端套件。

交付前已恢复 `backend/spareparts_backend.egg-info/` 的生成改动，删除本任务 `.field-protection-*.log`，并停止、删除指定容器 `itsp-laneB-fieldprot-20261020`；未清理其他容器。暂存区 `git diff --cached --check` 通过。

Alembic 图仍为唯一 head `c9d4f2a6b8e1`，没有新增迁移、历史回填或 schema 降级；完整测试夹具从空库 upgrade head，既有迁移回归继续保留。回退代码应保留账本数据，不能通过清空账本模拟回退；旧代码不具备本次备注保护能力，应暂停有冲突的原件更新并先准备修复候选。

开发验证不代表用户验收、main 审批或部署。本任务只准备本地提交及 PR 文案，不推送，不开 PR；用户点按和 @Jinchen-Yang 对候选 SHA 的人工审批另行安排。

## 文件清单

- `backend/app/services/maintenance_field_protection.py`：公共差异、账本及保护判定。
- `backend/app/services/maintenance_demand_manual.py`、`backend/app/api/maintenance_demands.py`：网页备注、保护字段输出、待核对 API、撤销说明及锁内版本校验。
- `backend/app/services/maintenance_project_master_workbook.py`：需求区最小接线、三位数量精度、真实身份变化判断。
- `backend/app/etl/loader.py`：共享保护判定、有效回执与审计、真实 PN 换绑的旧价退役。
- `backend/app/services/maintenance_wbdd_reconciliation.py`：复用人工新建身份识别。
- `backend/app/models/maintenance.py`：更新已有列的用途注释，无结构变更。
- `backend/tests/test_maintenance_field_protection_api.py`、`backend/tests/test_maintenance_field_protection_loader.py`、`backend/tests/test_maintenance_field_protection_workbook.py`：新增公共入口契约。
- `backend/tests/test_demand_manual_edit.py`：修正注释，保留测试。
- `CONTEXT.md`、本文：统一语言、勘察证据、实施及交付记录。

## 建议 PR 文案

标题：`feat: 按实际改动字段保护维保需求原件（ITSP-6）`

决策: D-34

修复总表只改备注或人工价就冻结整行需求的问题。网页和总表按归一化后的实际差异记录字段保护，loader 共用同一判定：未改数量继续接受原件，备注保留人工值，PN 文本与 part_id 成组保护，原样回传不产生新保护。

历史整行保护继续保留，并提供带权限和分页的只读待核对 API；人工新建行不会被来源行覆盖。既有撤销恢复首次人工修改前快照，并明确采用新原件需重传。保留事务、版本冲突和领用原语协议，补齐真实 PN 换绑停用旧价的审计。

验证：相关子集 303 通过；全量一次 5978 通过、7 跳过、1 环境失败（缺 ShellCheck），补齐本地工具后该项补跑 1 通过，无未解决失败。复用现有 JSONB 人工账本，无 schema 迁移或历史回填。范围仅 D-34 第 1、2 款；取消连带作废、ITSP-5 编辑工作台不在本 PR。
