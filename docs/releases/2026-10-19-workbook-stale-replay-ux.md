# 项目总表领用重放冲突候选验收

决策: 无

候选分支：`fix/workbook-stale-replay-ux`，开发基线 `54efe9d`。
状态：待用户点按验收、待 main 审批；没有合并或生产部署。

## 范围与兼容

根因及接口示例见[实施契约](../maintenance/2026-10-19-workbook-stale-replay-ux.md)。
汇总空实体ID行命中已有领用身份后的字段差异，最多展示 200 行及完整冲突数量；
HTTP 422、`stale_site_replay` 与 `message` 保持兼容。新增校验失败访问日志，
只记录项目、错误码与数量，审计故障不覆盖原响应。

身份/去重逻辑未改动。内部 issue_no、展示号、原样重放、实体ID三方合并、
作废优先与权限门禁保持原有契约。模板只更新说明文案，版本保持 2.7.1。
无数据库迁移，无数据修复或历史补录。

## 开发验证

全部业务测试使用合成数据；原始生产工作簿未复制进仓库、未提交。
后端使用本任务独占的临时 postgres:15，每个 pytest 进程另建独立数据库，
不连接共享的 :5433 或任何生产环境。
回归期间发现临时库的夹具 TRUNCATE 在 `DataFileImmediateSync` 等待超过一分钟，
仅在该可丢弃容器中关闭 fsync 后继续；真实 PostgreSQL 事务、约束、锁与并发检查照常执行，
本次不验证断电持久性。未改仓库环境文件、共享数据库或其他容器。
测试结束后已停止并删除本任务创建的 `itsp-wb-pytest-20261019-ux-17608` 容器。

- 新增 service 契约：11 passed。包括日期/是否应返还/多字段/地址差异、203 行截断、
  identical replay、generated display_no、三方合并、同文件重复和项目隔离。
- 新增 API 契约：9 passed。真实 HTTP 422 与访问日志持久化、JSON 日期字符串、
  日志总数不截断、审计故障隔离、普通错误、成功日志兼容及 scope 拒绝。
- 原首错行为和前端只显示 toast 的行为已通过先失败再通过的回归验证。
- 前端最终执行 `npx tsc && npx vitest run && npx vite build`，退出 0；
  119 个测试文件、1527 项全部通过，含上传组件 27 项；Vite 构建成功。
- 不同代理分别完成业务、前后端交叉及最终独立静态复核；均为同模型独立角色，
  没有把自查或自动测试当作用户验收。

- 完整相关后端回归覆盖 46 个文件：文件名含 `workbook`、`roundtrip`、`master_v2`，
  再并入直接引用 `maintenance_project_master_workbook` 的文件（含自动身份、展示号、
  权限、并发、地址、成本及相关迁移契约）。四批独立库分别通过 219、231、241、260 项，
  合计 **951 passed**，四批退出码均为 0。无跳过的相关文件。
- `git diff --check` 通过。日志中的 openpyxl、SQLAlchemy 弃用/迁移提示及 Vite 包体积
  提示没有导致检查失败；本次未顺带修改这些既有路径。

完整文件集的复现方法（在 `backend/`，数据库 URL 指向新建临时 PostgreSQL）：

```bash
mapfile -t cases < <(
  {
    rg --files tests | rg '/test_.*(workbook|roundtrip|master_v2).*\.py$'
    rg -l maintenance_project_master_workbook tests/test_*.py
  } | sort -u
)
PYTEST_DATABASE_BASE_URL="$WB_TEST_DATABASE_URL" uv run --extra dev pytest -q "${cases[@]}"
```

## 用户点按验收

1. 在候选验收环境下载项目总表，用合成的至少三条已有 WBDD+PN+SN 记录追加空实体ID行，
   分别只改日期、只改是否应返还、改多个字段；上传后应一次列出真实 Excel 行号、
   已有实体ID、单号/PN/SN 以及「原值 → 上传值」，不能强制接管。
2. 关闭弹窗后修正文件：已有记录保留实体ID在原行改；真正的新领用单号留空自动编号。
   再次上传应可用，原样重传不能新增重复事实。
3. 查看 00 与 98 页中的旧身份说明；检查超过 200 行时的总数和截断提示，
   窄屏、长 PN/SN 与多行备注可滚动阅读。检查切项目、退出或撤销权限时弹窗关闭。
4. 检查旧式无结构错误仍显示原消息；普通三方冲突保持既有流程；
   强制接管后的重校验若遇身份冲突，应转为只读差异弹窗。

## 合并、发布与回退

本任务只创建指向 main 的 PR，不执行合并或部署。main 合并及后续发布继续遵循
[分支政策](../BRANCH_POLICY.md)，用户点按验收和精确候选 SHA 人工审批不能由 CI 替代。
若后续获准部署，应使用同一 commit 的前后端构建，沿仓库既有备份、迁移链检查及发布流程。
本候选无新增迁移；回退前后端到上一批准 commit 即可，不需要改业务数据。
