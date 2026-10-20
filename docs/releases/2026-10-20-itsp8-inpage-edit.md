# ITSP-8 页内编辑候选验收与回退

决策: D-31

方案 A 由杨金臣于 2026-10-20 选定。本候选叠加在 #423 的 `0c98da7`
之上，审查请看该提交之后的增量。实施边界见
[方案](../plans/itsp8-inpage-edit.md)，接口与错误恢复见
[事实契约](../integrations/maintenance-site-issue-facts-v1.md)。

## 行为变化

项目面板领用明细新增编辑抽屉，允许 page_manual、workbook V1/V2 历史、
legacy 有效事实修改数量、SN、返还要求、备注、需求关联和 PN/part 身份组。
单号、日期、领收人、地址使用独立单头命令，明确“影响整张领用单全部行”；
只传改动字段、只读显示成本，不创建、删除或替换未提交行。

复用 #423 的实名权限、当前授权复验、锁序、双版本 CAS、幂等回执及
revision 链；原语负责取价及审计，单号/日期全单取价、PN 清旧人工证据，
未知价格不补零。原生来源和发货关联继续失败关闭；含有效发货关联行的整单
不开放单头修改。改号重号拒绝，不使用工作簿的隐式合单能力。

409 保留草稿并要求显式重读；结果未知只能原键原载荷核对。
已保存后刷新失败保留成功回执。旧人工整单编辑与批量三态入口继续保留。
稳定 search 增加可选 issue_id，用于改号后仍能准确重读。

## 开发验证

测试由指定 gate 准入，单进程串行执行；
使用容器 `itsp-laneA-itsp2-20261020`（127.0.0.1:32871），
pytest 夹具为每进程创建独占 PostgreSQL 测试库。未运行全量套件。

首轮 TDD：扩行字段四来源 4 例因旧白名单返回 422，单头 1 例因路由不存在返回 404；
接入外壳后这 5 例通过。前端从缺少编辑按钮和缺少冲突恢复的失败场景推进。
最终结果不把中间重复运行累计为独立用例。

后端最终 **179 passed、0 failed**（两次定向进程，61 + 118）：

| 定向文件（backend/tests/） | 通过 |
|---|---:|
| test_site_issue_line_correction_api.py | 52 |
| test_maintenance_beta_gate.py | 9 |
| test_maintenance_site_issue_inpage_edit_api.py | 64 |
| test_maintenance_site_issue_facts.py | 25 |
| test_maintenance_site_issue_facts_errors.py | 19 |
| test_maintenance_site_issue_facts_compatibility.py | 10 |

新 64 例覆盖 T1–T5、T9：各来源局部性、全单地址/取价、需求归属、旧抽屉/补价/工作簿
冲突、不同字段三路合并、实名审计、故障回滚、锁等待撤权和真实双连接行/头竞争。
首轮该文件 62 通过、2 个夹具失败：未归属夹具试图修改不可变归属历史、Excel PN
夹具使用非标准小写。分别改为从未归属的来源数据及大写标准 PN 后，最终 118 例组合
全部通过，未为修测试放宽业务规则。确认旧 V2 关联列仍为 422 的兼容场景也已通过。
仅有既有 Starlette/httpx 和 openpyxl 弃用提示；pytest 初始化迁移链正常。

前端最终 **7 文件、61 passed、0 failed**（132.25 秒）：

| 定向文件 | 通过 |
|---|---:|
| SiteIssueEditDrawer.test.tsx | 15 |
| SiteIssueWorkflowPanel.test.tsx | 10 |
| SiteIssueReturnBatch.test.tsx | 14 |
| SiteReturnTabRefresh.test.tsx | 8 |
| SiteReturnTab.test.tsx | 9 |
| siteIssueLineCorrection.test.ts | 3 |
| tests/sourceGuards.test.ts | 2 |

Vitest 使用 `--pool=forks --maxWorkers=1 --minWorkers=1 --no-file-parallelism`。
`npx tsc --noEmit -p .` 与 `npx vite build` 均退出 0；Vite 7.3.6 构建 19.57 秒，仅有既有大 chunk 提示。前后端源码在同一候选提交中，未运行全量套件或 CI。

同模型不同角色完成后端及前端独立静态复核，不称为异构审查。发现并修复
StrictMode 读取锁未释放、成功字段被当作未提交草稿再次带入两项前端问题；
后者行/头各一例先失败再通过，StrictMode 延迟及过期响应亦有回归。
未运行浏览器或实际用户点按，状态为待验收。

复跑命令（每条前必须先执行原 `gate.sh`，75 等 60 秒再试；不要并行）：

```bash
# backend/，测试连接通过 PYTEST_DATABASE_BASE_URL 指向上述隔离容器
uv run --extra dev pytest -q tests/test_site_issue_line_correction_api.py tests/test_maintenance_beta_gate.py
uv run --extra dev pytest -q tests/test_maintenance_site_issue_inpage_edit_api.py tests/test_maintenance_site_issue_facts.py tests/test_maintenance_site_issue_facts_errors.py tests/test_maintenance_site_issue_facts_compatibility.py
# frontend/
npx vitest run src/pages/maintenance/panel/__tests__/SiteIssueEditDrawer.test.tsx src/pages/maintenance/panel/__tests__/SiteReturnTab.test.tsx src/pages/maintenance/panel/__tests__/SiteReturnTabRefresh.test.tsx src/pages/maintenance/panel/__tests__/SiteIssueReturnBatch.test.tsx src/components/maintenance/__tests__/SiteIssueWorkflowPanel.test.tsx src/api/__tests__/siteIssueLineCorrection.test.ts tests/sourceGuards.test.ts --pool=forks --maxWorkers=1 --minWorkers=1 --no-file-parallelism
npx tsc --noEmit -p .
npx vite build
```

## 用户点按验收

由用户安排本候选精确 SHA 的前后端同构建验收，不要求合入 test。
代理自动化与静态审查不代替用户操作。

1. 有权实名账号对四类允许来源逐项编辑，验证长 PN、中文、窄屏与键盘可操作。
   无动作权限、范围外、原生/发货关联行不出现可用编辑入口。
2. 同单三行只编辑一行，核对其他行身份、有效性、原值不动；再改地址，
   核对整单生效且其他单不受影响。单头与行区分开保存、保留另一分区草稿。
3. 旧抽屉与其他窗口/工作簿互改 SN、备注、数量、PN、日期、地址；
   同字段冲突保留草稿且不自动重试，显式重读再确认。
4. 需求关联同项目通过，跨项目/无归属/不存在拒绝；清空与省略区别明确，
   不改实际收货关联。已有 V2 关联列只读限制保留。
5. 量、PN、关联、单号/日期取价结果符合既有规则；成本仅显示，缺价仍为空。
   地址/备注不因本次编辑额外重价。断网未知时原样核对，成功但刷新失败不重发。
6. 返还容量场景 T6–T8 由独立 #422 候选及其联合验收提供，本候选不宣称已验证该依赖。
   不交付 ITSP-3 返还状态文案。

## 迁移与回退

无 schema 迁移、历史回填或来源转换；不修改 workbook 主模块。
现有命令表兼容行/头的 action=correct 和不同指纹入口，#423 原行回执兼容。
旧 V1 文件保持其兼容限制，不宣称具有新命令的严格 OCC。

未推送、未合并、未访问或部署生产。正式发布仍需用户点按、CI、管理员对当前
SHA 的人工 APPROVED review 和明确部署授权；备份、前后端同 SHA、
alembic upgrade head、release 标签按现行 runbook 执行。

候选失败时停止交付。代码回退撤下本次入口，不删除成功事实、审计或回执；
业务反向更正须有权实名人员以当前版本执行，不能把代码回退当作数据撤销。
复用容器保持运行。

## 待金臣确认

页面改需求关联后，旧 V2 文件的既有行关联列可能按原只读校验返回
422 readonly_field_changed；本轮保留该兼容规则。是否后续调整为签名基线
比较另行确认，不在本轮修改高危工作簿主模块。
