# 访问审计 JSONB 序列化修复

- 来源：`e2e-audit-20261018` 的 MUST-FIX。
- 基线：`origin/main` / `54efe9d`；分支：`fix/e2e-audit-log`。
- 决策: 无。只修复审计写入缺陷，不调整业务口径、日志开关默认值或数据库结构。

## 根因与改动

`record_access_log` 原样将 `filters` 交给 `SysAccessLog.detail`（JSONB）。
采购分析 summary/orders 传入原生 `date` 时，驱动 JSON 编码失败，外层
best-effort 捕获异常后未写入审计行。`record_security_event` 有相同风险。
当前销售分析已自行调用 `str()`；本次保留调用端，统一修复写入边界。

`security.py` 新增纯转换函数，两条日志路径在赋值 `detail` 前调用：
日期/时间转 ISO 字符串，Decimal/UUID 等非 JSON 值转文本，集合排序后转数组，
递归保留字典/列表及空值，不修改调用方对象。再次转换同一 JSON 结果保持等价。
独立会话提交、连接关闭、列宽截断及异常类型告警保持原样；异常仍不打断业务请求。

## 验证与边界

- 最小复现：旧实现两条日期落库测试均 `NoResultFound`，`2 failed in 9.67s`；
  修复后同一测试 `2 passed in 9.80s`。
- 扩展回归覆盖嵌套日期/时间/Decimal/UUID/集合、原生 JSON/None、输入不变、
  重复序列化、严格 JSON 编码模拟、开关关闭、提交/转换失败隐私及连接关闭，
  并通过采购与销售分析四个 HTTP 端点回读审计行。
- 扩展回归结果：`216 passed, 1 skipped, 1 warning in 423.01s`；
  warning 为既有 Starlette TestClient/httpx 弃用提示，唯一跳过项已按下述命令补跑通过。
- 独立子代理静态复核未发现阻塞项（同模型不同角色，不代表异构审查）。
- 测试仅使用 conftest 在本地 Postgres `:5433` 创建的每进程独立数据库与合成数据。
- Nginx 隐私测试首次因本地缺固定镜像而跳过；拉取测试要求的固定 digest 后补跑
  `uv run --extra dev pytest tests/test_runtime_log_privacy.py::test_frontend_api_fault_log_does_not_persist_query_values -q --tb=short -rs`，
  结果 `1 passed in 20.94s`。
- 循环引用或文本转换本身抛错仍进入 best-effort 告警；不承诺记录任意畸形对象图。

扩展回归命令（仓库根目录执行）：

```bash
cd backend
uv run --extra dev pytest \
  tests/test_access_log_serialization.py \
  tests/test_access_log_action_width.py \
  tests/test_access_log_action_width_migration.py \
  tests/test_audit_trail.py \
  tests/test_runtime_log_privacy.py \
  tests/test_purchase_analytics_api.py \
  tests/test_sales_analytics_api.py \
  tests/test_maintenance_project_operations_api.py::test_operations_search_access_log_excludes_query_content_and_derivatives \
  -q --tb=short
```

## 交付状态

本地针对性回归与独立静态复核通过；未运行全量后端套件或前端构建。
本次无迁移、无前端改动，不触碰生产；
PR 指向 main，合并仍需遵循 `docs/BRANCH_POLICY.md` 的人工审批，部署需另行授权。
回退为撤回本次代码提交，无需数据库回退；历史丢失的审计行无法由本修复补建。
