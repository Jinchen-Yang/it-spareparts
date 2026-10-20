# D-34 字段保护自动解除

决策: D-34

基线 `58201e4ca843af197fd8eb4a35b264cc581c22b5`；分支 `fix/field-protect-auto-release`。
fetch 后 origin/main 与基线一致。仅本工作树开发、本地提交，不推送、不建 PR、不连接生产。
依据金臣 2026-10-20 11:08 补充裁定；不增加迁移，不运行后端全量。

## 勘察（基线文件:行号）

- `backend/app/services/maintenance_field_protection.py:48`、`:73`：记录人工变更时保留首次 `source_value`；改回仍写账本，不解除。
- `backend/app/etl/loader.py:421`：skip 只新增；`:439` 读取账本；`:454–464` 的 upsert 仅更新业务列，不更新账本。**受保护期间 source_value 不刷新**；来源 2→人工 5→来源 9 后仍记 2。
- `backend/app/services/maintenance_demand_manual.py:395`：网页共用记录 seam；`:405` 在同事务记录完整前后快照。`:672–709`、`:731` 的显式撤销恢复首次快照，现有行为和文案需要保持。
- `backend/app/services/maintenance_project_master_workbook.py:1306`、`:6918`：V1 备注与 V2 需求更新均走公共 seam。`:1315`、`:6890` 的审计前值尚未包含旧账本，解除时需补齐。
- `backend/app/services/maintenance_project_master_workbook.py:1311`：V1 备注审计直接索引项目映射，缺项会 KeyError；`:3318–3347` 的数值解析先量化后验范围，大数可抛 InvalidOperation。
- `frontend/src/pages/maintenance/DemandLinesEditor.tsx:42`、`:273`：保护标识按账本存在性显示，写后重新读取；无需改变前端。显式撤销的“首次”说明继续成立。

## 三路分析与实施

用户场景：有权实名人经网页或导出/回传改回来源，随后合法原件可覆盖。既有权限、digest、工作簿版本、锁与事务继续由入口负责；错误返回可读回执。

业务边界：公共 seam 按归一值比较；PN 三成员全部匹配才解除，未知来源证据不猜。显式空值是人工事实；历史非 wbdd 与人工前缀仍整行保护。成本、领用与返还不引入新规则。

技术方案：合法 upsert 更新受保护字段的 `source_value`（skip 不更新），同时保存 `original_source_value` 供显式撤销恢复首次快照。来源更新与账本刷新在既有事务中留审计，不把被挡来源值冒充已覆盖业务值。人工解除复用既有编辑审计，记录旧/新账本并标明“改回来源值，保护解除”。不做历史转换。

仅来源快照变化也推进项目 revision、改变网页 digest；旧网页修改得到 409，旧工作簿遵循既有 2.7.0 行级合并契约，在回执标明 `revision_drift`，不新增整本硬拒规则。历史 PN 缺来源成员时不猜来源；合法 upsert 补齐最新组，首次未知成员标记 `original_source_value_known=false`，显式撤销继续保留该成员现值。

两个非阻塞建议均可小改：V1 备注解析项目 ID 使用 `.get()` 并抛已有 `project_scope_denied`；数值量化前检查有限性/量级，保留量化后范围检查，返回 `WorkbookError`。

## 公共 seam 与验证

用户已授权自主选取既有公共边界：网页 HTTP PATCH/GET、工作簿 build/validate/apply、loader.load、公共字段保护服务。先失败测试再最小实现，所有测试主代理串行运行，无 xdist。数据库使用独立 PostgreSQL 15 容器 `itsp-laneB-protrelease-20261020`，仅绑定 127.0.0.1 随机端口；长命令 nohup，日志 `~/workspaces/reviews/lanes-20261020/laneB/protrelease-*.log`。

覆盖：双入口 2→5→2→来源9、持续保护/改第三值、NULL 和空来源、PN 部分/全部还原、等值/原样回传、解除审计、最新来源/skip、显式撤销首次兼容、手工/legacy 保护、工作簿项目缺项及大数回执。保留原有相关契约，最终跑相关子集与 ruff，全量交 CI。

## 待金臣确认

首次与最新来源存在差异：来源2→人工5→合法来源9→人工2。当前按任务要求选**最新一次合法来源值**，因此人工2仍受保护，人工9才解除。另一选项是始终以首次来源2为比较基准，则人工2解除而人工9仍保护。本候选暂按最新值实现，不把这一暂行选择写成已获额外裁定。

旧账本没有保存被挡过的历次来源值，不能从现值猜最新值；下一次合法 upsert 后才获得最新来源证据。无迁移或历史回填。

## 验证与交付记录

测试库仅使用容器 `itsp-laneB-protrelease-20261020`，绑定 `127.0.0.1:32883`；每轮由 conftest 创建独占库并从空库迁移到既有 head。所有 pytest 串行，无 xdist。

红绿证据（日志均位于 `~/workspaces/reviews/lanes-20261020/laneB/`）：

- `protrelease-red-first.log`：3 failed，网页改回来源仍保护，V1 项目映射缺项抛 KeyError/TypeError。
- `protrelease-cycle2.log`：3 passed / 1 failed；首轮修复转绿，最新来源用例失败在 source_value 仍为2而非9。
- `protrelease-red-pn-workbook.log`：8 failed / 10 passed，复现 PN 全组不解除、工作簿缺解除审计标识、大数/Infinity/sNaN 量化抛 InvalidOperation。
- `protrelease-core.log`：核心5文件 **74 passed**，220.22秒；包含原有30例与当时新增44例。
- `protrelease-red-history.log`：1 failed；独立审查发现首次 PN 身份未知时，显式撤销把最新来源身份3当首次身份（应保留当前2）。修复为首次明确未知时不回退 peer 的最新来源身份，另加 legacy/三人工前缀人工改回值仍整行保护4例。

最终新增 **49 个参数展开用例**（网页6、工作簿5、来源刷新/历史兼容22、输入错误16）；未删除既有测试。

最终相关子集命令（backend 目录）：

```bash
PYTEST_DATABASE_BASE_URL=postgresql+psycopg://spareparts:spareparts@127.0.0.1:32883/spareparts_test \
uv run --extra dev pytest \
  tests/test_maintenance_field_protection_api.py \
  tests/test_maintenance_field_protection_workbook.py \
  tests/test_maintenance_field_protection_loader.py \
  tests/test_maintenance_field_protection_source_refresh.py \
  tests/test_maintenance_workbook_input_errors.py \
  tests/test_demand_manual_edit.py \
  tests/test_demand_manual_p1_guards.py \
  tests/test_demand_manual_api_guards.py \
  tests/test_wbdd_upload_feedback.py \
  tests/test_maintenance_project_master_v2_editable.py \
  tests/test_maintenance_master_v2_lock_freshness.py \
  tests/test_maintenance_project_master_workbook.py \
  tests/test_maintenance_workbook_revision_invalidation.py \
  -q --tb=short --disable-warnings
```

整批日志 `protrelease-final-subset.log`：**307 passed / 2 failed**，739.46秒。两项失败均为既有 `test_wbdd_upload_feedback.py` 仍断言受保护来源快照变化不推进 revision；本次补充裁定采用最新来源后，快照改变必须推进 revision。保留业务字段实际覆盖数、有效值审计、人工数量及真实并发锁等待断言，更新 revision 预期并增加独立来源快照审计断言。随后只重跑完整 feedback 文件（生产代码未再变化）：

```bash
PYTEST_DATABASE_BASE_URL=postgresql+psycopg://spareparts:spareparts@127.0.0.1:32883/spareparts_test \
uv run --extra dev pytest tests/test_wbdd_upload_feedback.py -q --tb=short --disable-warnings
```

独立代理分别完成场景/业务/技术勘察及实现复核（同模型不同角色，不声称异构审查）。未知首次 PN 身份问题经红测修复后复核无剩余阻塞；两项版本断言更新也已独立复核，没有削弱原契约。

Ruff 0.17.1：公共字段保护模块及五个变更测试文件全部通过。对所有10个变更 Python 文件与 `58201e4` 同配置逐文件对比，基线68项、当前49项、**新增0项**（`protrelease-ruff-baseline.log`）；剩余49项均为 loader、模型、网页服务、工作簿服务的既有告警，全文件检查不是零告警。没有为消除这些存量告警扩大共享文件改动。`git diff --check` 通过。检查命令：

```bash
uvx --offline ruff check \
  app/services/maintenance_field_protection.py \
  tests/test_maintenance_field_protection_api.py \
  tests/test_maintenance_field_protection_workbook.py \
  tests/test_maintenance_field_protection_source_refresh.py \
  tests/test_maintenance_workbook_input_errors.py \
  tests/test_wbdd_upload_feedback.py
```

`protrelease-feedback-green.log`：**11 passed / 0 failed**，37.19秒，退出码0。两轮合计为13个相关文件的 **309个不同用例均取得通过证据**，其中新增49例；不是声称一次整批309绿。无未解决失败。测试输出只有既有 Starlette/httpx 与 openpyxl 弃用提示；未跑本地后端全量或前端套件，全量留 CI。

交付前已停止并删除本任务唯一专用容器 `itsp-laneB-protrelease-20261020`，未操作其他容器；已恢复生成的 `backend/spareparts_backend.egg-info/`，日志只保留在指定 reviews 目录，不纳入提交。

## 兼容与交付边界

无 schema 变化、迁移或历史批量处理，前端无需改动。历史被挡来源值无法补猜，重传合法原件后才更新来源快照。回退旧代码会使旧显式撤销按已更新的 `source_value` 还原最新值；不能声称其仍恢复首次值。需要回退时保留账本及本次首次快照读取适配，或暂停相关编辑/撤销/受保护原件更新并准备修复；不通过清空账本回退。

本地验证不代表用户验收、CI、main 审批或部署。仅本分支提交，用户另行安排验收及 PR；main 合并仍须 @Jinchen-Yang 对当前 SHA 的人工 APPROVED review。

## 文件清单

- `backend/app/services/maintenance_field_protection.py`：归一化解除、PN 组与来源快照公共逻辑、审计原因。
- `backend/app/etl/loader.py`：最新合法来源快照、独立审计、metadata revision。
- `backend/app/services/maintenance_demand_manual.py`：网页解除审计、首次快照撤销兼容。
- `backend/app/services/maintenance_project_master_workbook.py`：V1/V2 审计及两项最小输入错误修复。
- `backend/app/models/maintenance.py`：已有 JSONB 账本注释，无 schema 变化。
- `backend/tests/test_maintenance_field_protection_api.py`、`backend/tests/test_maintenance_field_protection_workbook.py`：双入口回归。
- `backend/tests/test_maintenance_field_protection_source_refresh.py`、`backend/tests/test_maintenance_workbook_input_errors.py`：来源/历史兼容和错误回执回归。
- `backend/tests/test_wbdd_upload_feedback.py`：保留原反馈及并发断言，更新来源快照 revision 预期并补审计。
- `CONTEXT.md`、D-34 决策文档、本文：裁定、口径及证据。

## 建议 PR 标题与正文

标题：`fix: 改回来源值时自动解除维保字段保护（D-34）`

决策: D-34

落实金臣 2026-10-20 补充裁定：网页与工作簿人工改回归一化来源值时自动解除字段保护，2→5→2 后来源9生效。PN 身份组三成员全部还原才解除；显式空值按来源是否为空处理，历史整行保护与人工新建行继续保留。

受保护期间合法 upsert 刷新最新来源快照，来源更新本身不解除保护；首次快照独立保留供既有显式撤销使用。来源快照刷新及自动解除均留前后账本审计，并沿既有协议更新 digest/revision。首次或最新来源的选择暂按任务要求取最新，另一选项详见实施计划待金臣确认。

顺带将工作簿项目映射缺失及大数/非有限值量化错误转为可读 WorkbookError。无迁移、无历史批量回填、无前端修改。

验证：新增49例，13个相关文件309个不同用例分轮验证通过（首轮307通过、2项旧revision预期失败；更新预期并保留/加强原断言后，完整feedback文件11通过）。公共模块及五个变更测试文件Ruff通过；全变更文件无新增Ruff诊断，四个既有大文件仍有49项基线告警。独立静态复核无剩余阻塞；本地全量未跑，留CI。仅本地提交，未推送、未建PR、未部署，待用户验收及管理员对候选SHA人工审批。
