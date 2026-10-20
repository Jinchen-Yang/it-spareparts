# ITSP-2 批量返还要求候选验收与回退

决策: D-31

本候选实现项目面板领用明细的三态批量更正，沿 D-32 保留标准硬盘规则及免返文本提醒含义。技术契约见 [单行网页调用方](../integrations/maintenance-site-issue-facts-v1.md#网页单行更正调用方itsp-2)，测试场景见 [实施方案](../integrations/itsp-2-batch-no-return-plan.md)。本文件不是部署授权。

## 本轮开发验证（2026-10-20）

基线 `319e25f6cd2b2c0119a0e865893a38a6cf9ca362`，仅当前 `feat/batch-no-return` worktree。使用既有专用 PostgreSQL 容器 `itsp-laneA-itsp2-20261020`，localhost:32871；每个 pytest 进程仍由仓库隔离夹具建立独立测试库。每次测试前检查 `uptime` / `free -g`，按文件顺序单进程执行，未运行本地全量 pytest / Vitest。

后端定向 **152 passed、0 failed**：

| 测试文件（`backend/tests/`） | 通过 |
|---|---:|
| `test_site_issue_line_correction_api.py` | 52 |
| `test_maintenance_site_issue_facts.py` | 25 |
| `test_maintenance_site_issue_facts_errors.py` | 19 |
| `test_maintenance_site_issue_facts_compatibility.py` | 10 |
| `test_site_issue_v2_api.py` | 18 |
| `test_manual_site_issue_api.py` | 18 |
| `test_native_workbook_return_requirements.py` | 4 |
| `test_site_return_requirements.py` | 6 |

逐文件命令为 `PYTEST_DATABASE_BASE_URL=<专用测试连接> uv run --extra dev pytest -q tests/<文件>`。新 API 的 52 项覆盖允许来源三态、null 与省略、同单版本接续、幂等回放、权限/范围、真实双连接竞争、等待锁时撤权、审计/outbox/回执故障回滚及旧工作簿冲突。仅观察到既有 Starlette/openpyxl 弃用提示。模型和迁移未改，不另跑 alembic check；pytest 夹具的迁移初始化正常执行。

前端首轮已有用例复现两处未完成行为（9 passed、2 failed）：关闭未知结果后无法恢复、原因缺少长度校验。补齐恢复入口、未知结果防覆盖及 1000 字符校验后，追加会话变化与未知回执再次被拒的覆盖。6 文件回归最初为 53 passed、1 failed；失败来自测试全局 PN 查询同时命中领用列表与保留的隐藏结果表，限定查询区域后单独复跑该文件 14 passed。按各文件最终结果合计 **54 passed、0 failed**，不重复累计复跑：

| 前端测试文件 | 通过 |
|---|---:|
| `SiteIssueReturnBatch.test.tsx` | 14 |
| `siteIssueLineCorrection.test.ts` | 1 |
| `SiteReturnTab.test.tsx` | 9 |
| `SiteReturnTabRefresh.test.tsx` | 8 |
| `ReturnReceiptBatchMaintenance.test.tsx` | 16 |
| `ReturnReceiptsSectionBatchMaintenance.test.tsx` | 6 |

Vitest 参数统一为 `--pool=threads --maxWorkers=1 --minWorkers=1 --no-file-parallelism`。`npx tsc --noEmit -p .` 与 `npx vite build` 均退出 0；构建 26.41 秒，仅有既有大 chunk 提示。

完成后端、前端及领域三路同模型独立静态复核；不称为异构模型审查。本轮跳过 Playwright/浏览器，UI 仅由组件测试验证，仍需用户点按验收。全量套件由后续 CI 执行；本轮仅本地提交和 PR 草稿，未推送、未创建 PR、未触发 CI。专用容器保留，由操作者清理。

## 候选验收

由用户安排基于本 PR 精确 SHA 的候选构建实际点按。前后端使用同一 commit，不要求合入 test；开发自动验证或代理浏览器调试不替代此步骤。

1. 用具有既有领用维护动作、成本可见资格和项目范围的实名账号进入项目领用明细；勾选包含同单两行、不同来源和不同原始三态的行。
2. 点击「批量设为应返」，填写原因后确认，核对「是否应返还：是」；查看逐行结果，确认未变化行跳过。
3. 分别验证「批量设为免返」与「恢复按规则判断」；免返对应「否」，恢复后的标准硬盘按规则显示免返。显式应返硬盘仍保持应返，未选行保持不变。
4. 用另一窗口更正其中一行，再提交旧选择；冲突行应要求重新读取核对，已成功行保持成功，不把整批描述成回滚。
5. 核对数量、PN、成本、实际收货及项目免返标注不变，原收货属性批量继续可用。不出现本次新交付的已返／未返或收齐状态。
6. 先下载 06 工作簿，再网页更正同一字段，回传旧基线的相反更改，应提示冲突。未触碰字段仍按原三路合并规则处理。
7. 无动作权限或范围外账号不得执行；原生发货行保持不可选。用窄屏核对按钮、原因输入、结果表可操作；刷新失败不能把已保存的行说成失败并重发。

## 迁移与回退

无 schema 变更，无历史业务回填，不需要新增数据迁移。正式发布继续遵循仓库发布规则：管理员人工批准当前 SHA、明确部署授权、备份、同 commit 镜像、迁移链完成后启动及 release 标签；本任务不执行这些生产动作。

若候选验证失败，停止交付并保留逐行回执。可撤回本 PR 的入口和 UI 代码；已经成功保存的更正、审计和命令回执保留。业务撤销由有权实名用户以当前版本重新做反向更正，不直接改库、不删除审计、不假定代码回退会恢复事实。
