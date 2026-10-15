# 需求00009：项目备注候选、迁移与回退

决策: D-32

本次按金臣2026-10-15 16:58Z裁定新增独立项目备注 `project_note`（最多500个Unicode字符）。与既有免返标注 `no_return_note`（2000字符）并存。项目备注只作人工标记，不参与免返、领用、返还、收货、成本或其他业务计算。D-32与00009台账的补充由PM另开docs PR；本文件描述本次实现与运行方式，不改写既有决策。

基线 `d92da159a1ecb738d4436876d2a366cebdfcaee5`，分支 `feat/req00009-project-note`。这是开发候选，待用户点按验收、PM提交及PR、管理员对精确候选SHA的人工GitHub APPROVED review。没有生产部署授权；未提交、未推送、未创建PR、未合并、未部署。

## 读写、权限与兼容

- 概览基础信息单独显示“项目备注”，可从“编辑项目备注”进入既有基本信息弹窗。两个文本框独立，未触碰不提交；空白或null明确清空。服务端先校验原串最多500字符再trim，保留内部换行，不截断。前端按Unicode code point计数，500个emoji可完整保存。
- PATCH复用#371主档实名、page/action/data_profit、项目范围、active、version CAS、state→project锁、主档before/after审计和workbook revision。409保留当前弹窗草稿；草稿仅在当前表单内存，刷新页面不恢复。
- signed `01_项目概览` 追加黄色“项目备注”单元格，回传写同一个DB字段。签名元数据增加JSON基线与主档版本，验签后只解析此新增字段；不扩展项目名称、人员、期限等其他概览字段回写。既有合同金额回写保留。
- 旧签名模板没有新增基线时整个备注字段保持。新模板缺备注行或有重复行422拒绝；空白明确清空；超长或公式单元格中文拒绝。导出=开头的文字保持文本，不执行公式。
- 预检只识别签名基线相对修改意图，不承诺提交时版本仍有效。实际变更须在validate/apply两个入口额外通过主档管理、经营数据和实名门禁；原整本编辑权不自动取得备注编辑权。apply先查同账号、同文件、同export的幂等回执，再在锁内严格检查主档version；强制接管不能覆盖项目备注冲突。冲突整本零写，中文409提示保留文件草稿并重新下载核对。前端409保留所选文件并提供重试/取消，成功、取消、权限撤销或账号切换即释放内存文件。
- 主档审计与其他工作簿变化在同一事务，一次事务只bump一次revision；重放返回原回执，不撤销后来修改。项目列表导出新增可选“项目备注”，原默认六列不变，仍按既有范围过滤和文本转义。

## 签名工作簿配置前置

本功能的Excel路径为既有V2签名协议。仓库config、Compose及.env.example中 `MAINTENANCE_PROJECT_MASTER_V2_ENABLED` 默认false；本次未改变默认开关，也未访问生产读取实际取值。独立开发/验收候选须明确配置 `MAINTENANCE_PROJECT_MASTER_V2_ENABLED=true`，重新启动该候选后核对下载文件为signed `01_项目概览` 且含“项目备注”行。开关关闭时仍走旧V1协议，项目备注仅能从网页维护，旧V1文件不会写入该字段。未来发布须由PM核对候选与目标的现有V2配置，不能把V1上传放宽成无签名备注写入。

## 迁移及发布准备

新增revision `a40c9e1d7b52`，down_revision=`f307604fa4d7`。只新增 `maintenance_project.project_note TEXT NULL`，无默认值、索引或历史回填；`no_return_note`不变。迁移SET LOCAL lock_timeout=5s，遇锁失败即停止并查明原因。

开发期先在本任务独立PostgreSQL15临时库演练upgrade→downgrade父版本→upgrade，核对单head与alembic check。另以非空备注确认downgrade被拒、文字/列/版本表保留。不要把schema downgrade当默认回退。

未来明确获授权发布时：

1. 固定已获管理员当前SHA人工APPROVED的main候选，记录前后端同commit镜像、schema旧版本、上次镜像与release标签。
2. 在独立副本验证迁移、双入口、并发、权限和回退；执行可读取的数据库备份并核对备份与审计留存。没有备份验证即停止。
3. 对明确的目标数据库执行 `cd backend && uv run alembic heads`，须唯一head `a40c9e1d7b52`；再执行 `uv run alembic upgrade head`，核对 `uv run alembic current` 与 `uv run alembic check`。成功后才启动同一commit的前后端镜像。
4. 用有权、无权和双用户分别回读；确认项目备注与免返标注并存、页面/Excel一致、原默认导出列不变，领用/返还事实无变化。失败停止切流或回退代码，保留数据。
5. 记录精确SHA、镜像、schema、备份、操作人、验收和观察结果，按仓库规则打 `release-` 标签。合并不等于部署。

以上是未来授权后的runbook，本次未执行生产步骤。

## 开发验证入口

后端全量还会运行部署脚本契约，需要本机可执行的 `shellcheck`；它是测试工具，不是应用运行依赖。本次环境缺工具的同一失败已在干净main复现，局部补齐官方v0.11.0后该用例重跑通过，原全量退出记录仍须如实保留。

遵循 `.claude/skills/run-it-spareparts/SKILL.md` 的隔离测试方式，conftest每pytest进程创建随机独占数据库和raw目录。后端相关契约：

```bash
cd backend
uv run --extra dev pytest -q tests/test_maintenance_project_note.py tests/test_maintenance_project_note_workbook.py tests/test_maintenance_project_no_return_note.py tests/test_maintenance_project_export.py tests/test_maintenance_project_management_api.py
uv run --extra dev pytest -q
uv run alembic heads
```

前端相关组件与全量测试、类型和构建：

```bash
cd frontend
npx --no-install vitest run --maxWorkers=1
npx --no-install tsc
npx --no-install vite build
```

最终实际退出码、计数、独立复核及环境证据见 `R/logs/S-P-note.report.md` 与 `S-P-note-*.log`（R为本次PM指定交付目录）。开发测试和自动浏览器证据均不替代用户实际点按验收。

## 用户验收

1. 同时填写两个文本框，保存/刷新后互不覆盖；只修改项目备注时不提交免返标注。取消重开取服务器值。
2. 中英文、换行、HTML字面及500个emoji保存一致，501字符页面/API/Excel均明确报错，无截断；清空后显示未填写。
3. 有权账号通过signed 01修改备注，概览刷新为同一内容；页面修改后重新下载工作簿显示相同文字。
4. 两人使用同一主档版本，先保存的一方成功，后一方409保留草稿；Excel强制接管也不能越过备注CAS。原文件不会被浏览器改写。
5. 可看不可改、只有整本上传权、跨项目、非实名或停用账号不能借Excel写备注。
6. 旧模板原样上传不清空备注；新模板缺/重复备注行、签名篡改、公式或超长拒绝且其他表零写。
7. 项目列表导出默认列不变，手动勾选项目备注后导出该列；纯文本、范围与权限正确。

## 回退

常规回退前后端同一旧commit，保留加法列与全部备注/审计，旧代码忽略新列。冻结新写入口后可回读核实，不能用整库恢复覆盖回退期间其他业务。

确需移除列时另行取得操作授权，先导出并核实备注与审计保存，再按受控版本化操作处置备注。迁移在同事务独占表锁内检查，任何非空 `project_note` 都阻止downgrade；不得修改迁移或直接DROP绕过。空列副本才可演练 `uv run alembic downgrade f307604fa4d7` 后确认既有 `no_return_note`仍在，再 `uv run alembic upgrade head`。
