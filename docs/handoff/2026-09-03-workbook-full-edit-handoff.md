# 交接：维保总表全量编辑能力恢复（明早部署）

- 日期：2026-09-03 凌晨
- 仓库：`Jinchen-Yang/it-spareparts`
- 交接对象：zcode（以及任何接手「明早要上」的人）
- 代码基线：**GitHub `codex/master-workbook-full-edit` 分支最新 HEAD（`a876644`），不是 main，不是本地**。

---

## 一、先看这份的结论

**代码已经写完，不是明早才写。** 你要做的是：确认 CI 绿 → 合入 main → 部署上线。功能本体（行级合并、强制接管、权限下放、回执、审计）在 PR #304 里已经实现并推送，GitHub 上就是最新代码。开工前先 `git fetch`，别在旧 ref 上干活。

三条口径别做反了（业务已拍板，全文见本仓 `docs/decisions/`）：

1. **删行 = 作废**，02~06 五张表统一；被作废的行，别人改了**不复活**。
2. **上传不整本拒绝**。只校验用户改过的行；同行冲突才提示，可强制接管；不同行并发互不阻塞。
3. **非标准订单号一律拒绝导入**（氚云订单号固定 `XSDD-8位日期-4位序号`，人填不了）。

---

## 二、分支与 PR 现状（截至 09-03 01:40）

| 项 | 状态 |
|---|---|
| 功能分支 | `codex/master-workbook-full-edit`，HEAD `a876644`，**已推 GitHub**（本地=远端，无未推提交） |
| PR | **#304**「总表全量编辑恢复」，base=main，MERGEABLE，review=REQUIRED |
| 最新 CI | run 33659463322：前端类型+构建 **pass**；后端 pytest+迁移链 **pending**（~35 分钟一轮） |
| 上一轮 CI | 3 个测试失败，正是 `a876644` 修的那 3 个：`test_maintenance_salesperson_override_migration`、`test_maintenance_wbdd_display_columns_migration`、`test_maintenance_project_master_v2_editable`（迁移头断言旧值 f6b1，现应为 a8e4 单头） |
| 生产在跑 | `9b30126`（fix/contract-total-inc-tax 分支），**其 3 个提交 a29a4eb/80b54c4/9b30126 已全部是功能分支的祖先**——合入 #304 即覆盖生产代码，不会丢生产改动 |
| main 现状 | `c9d67fe`（PR #300 已合） |

### 8 个提交 = PR-1/2/3（对应 zcode 计划 4 个 PR 的前 3 个）

| commit | 内容 |
|---|---|
| `a29a4eb` `80b54c4` `9b30126` | 生产带过来的：回填 47 个项目期限 + a8e4 迁移链到 f6b1 单头 |
| `2764967` | PR-1 核心：行级三路合并取代整本作废（模板 2.6.0→2.7.0） |
| `e9ecc52` | PR-2：负责人/销售编辑权下放 + 失败审计 + 合同额放开 |
| `f35b104` | PR-3 前端：冲突三值对照 + 强制接管 UI + 字段级回执（1.27.0） |
| `6029c21` | CI 修复：force 冲突语义 / 删行空单元格 / 权限谓词 / 测试时序 |
| `a876644` | 修迁移头断言（f6b1→a8e4）+ v24 共享合同新口径 |

---

## 三、明天要做的：PR-4（生产对齐与上线）

按 zcode 计划的 PR-4，逐条：

1. **等 CI 绿**：后端 pytest 全绿（迁移链验证 conftest 会先 `alembic upgrade head`，顺带验了 a8e4 单头）。若还红，看 `test_maintenance_salesperson_override_migration.py:24` 与 `test_maintenance_wbdd_display_columns_migration.py:32` 的 heads 断言是否又落到旧值。
2. **合入 main**：走 PR 正常流程（base=main，review 通过）。合入后 main 应含 `a8e4f1c7d3b9` 单头。
3. **生产迁移前核对**：生产 `alembic_version` 现在停在 `f6b1d3e8a2c4`，但 a8e4 的期限回填**已经物理执行过（45 个项目）**。a8e4 是幂等的（`WHERE period_from IS NULL AND period_to IS NULL`），重跑安全；但更稳的做法是：**先确认生产已有数据的项目 period 是否已回填，再决定 `alembic upgrade head` 还是 `alembic stamp`**。
4. **开总闸**：`MAINTENANCE_PROJECT_MASTER_V2_ENABLED` 当前 `.env.example` 和 compose 默认 `false`。部署时设 `true`。这是新工作簿的开关，别漏。
5. **发版规范**（事故教训，一条都不能省）：
   - 从 **main** 部署，不是功能分支；
   - 前后端**同一个 commit** 构建（别前端旧后端新混搭）；
   - `alembic upgrade head` 跑完再启新镜像；
   - 打 `release-<commit>` 标签；
   - 迁移前备份。
6. **观察两个 cron 周期**：`sys_access_log` 里 `workbook_validate/workbook_apply/workbook_apply_takeover/workbook_apply_conflict` 有没有正常落；409 率是否下降。

---

## 四、验收检查（照 zcode 计划，逐条过）

1. 负责人/销售账号：下载→任意表改值/删行/加行→上传→回执逐字段列出→卡片墙/面板数字即时变化。
2. 两个账号同时改同一项目**不同行**→互不阻塞，各自成功（现 409 场景应回归 200）。
3. 改**同一行**→后传者看到三值对照冲突→可强制接管→接管明细入回执与审计。
4. 导入发生后再上传**未触碰行**→不再 409（rebase 生效）。
5. 每次下载/上传/校验/拒绝/接管在 `sys_audit_log` 可查（含失败）。

---

## 五、已知风险与坑

1. **模板 2.6.0→2.7.0 强制重下载**：旧文件因缺基线令牌列会被令牌校验拒绝，提示重新下载。会打到客户，属一次性迁移成本，提前在客户群里说一句。
2. **分支里混进无关文件**：`.deploy/Caddyfile.it-data.example`、`docs/releases/https-ingress-runbook.md`、`backend/tests/test_https_deployment.py`、`outputs/contract-amount-import-20260827/`（含 ndjson 3937 行 + 2 张 png）。合入前**先确认这些要不要跟着进 main**——它们跟"全量编辑"无关，建议从 #304 里摘出来或单独 PR，别让功能 PR 夹带 HTTPS 部署变更。
3. **`01_项目概览`的主档字段（名称/期限/负责人）在工作簿内编辑本次不做**（PR 304 描述里明确「后续迭代」）。期限缺失的治理走「销售订单导入建项」那条线（PR #302），不是这条。别把两条混了。
4. **本机 macOS 跑不了后端测试**（`tests/run_isolation.py` 要求 Linux）：以 CI 为门，前端 vitest 本机可跑。
5. **PR #302**（sales XSDD 建项，`codex/sales-xsdd-auto-project`，233 文件）和 #304 有 2 个测试文件重叠，**不要同时合**；#302 应该切小 PR 分批合，别整体合 5.8 万行。

---

## 六、口径与决策出处

8 条已拍板口径的全文在 `docs/decisions/`（本轮新提交）。最急的 5 条：

- D-01 删行=作废（02~06 五张表）
- D-02 上传不拒绝，行级校验，覆盖留回执；被作废行不复活
- D-03 负责人全字段可改（含成本合同额）；销售限本人项目、成本合同额受利润键控制
- D-04 销售订单导入建项（从 #302 切小 PR）
- D-05 维保项目唯一正规来源 = 销售订单导入；期限/名称/负责人从订单列取；WBDD 只挂靠不建项；封手工建项入口

参考文件：

- 事故调查：`outputs/2026-09-02-全量修改能力-事件调查与需求差异报告.md`
- 决策报告：`outputs/2026-09-03-重构决策审计报告.md`（十维审计结论：不整体重构，定向整改）
- 十维审计全文：`outputs/kimi-audit/results/orchestrated.md`
- 销售订单字段口径（含维保起止日期列）：`docs/reference/sales-order-columns.md`（本轮新提交）
