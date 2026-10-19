# 2026-10-19 e2e 性能与并发修复交接

决策: 无

范围：`fix/e2e-perf-concurrency` 基于 `origin/main@54efe9d55be2e908ce2a2f0182d1e4c57df8afad`，仅后端性能、请求数据库超时和验证工具。未改默认日期、业务口径、前端、访问日志实现、密钥或环境配置；无数据库迁移。状态是候选验证/PR，未合并、未部署生产。

## 原因与实现

- 维保逐单导出原本已有server-side cursor、write-only工作簿、临时文件和64 KiB下载块；慢在下载开始前的XML单元格构造。旧容器采样8971条明细，带分析器开销的73.11秒中，append占51.08秒，格式构造10.54秒、ORM加载2.71秒、ZIP压缩3.81秒，未发现逐行SQL。只优化ORM/Cell后的全量仍需78.20秒，因此换成XlsxWriter `constant_memory`，保留临时文件、有界输出、源共享锁，查询只投影需要的列。单次导出复用权限掩码，并使用公开的按类型写入API，避免逐行重新归一权限和逐格通用分派。
- 无筛选仍导出全部有效订单（含无日期单），显式宽范围不缩窗，不静默截断。原日期成对/倒序422、容量保护、权限、双税成本、人工补价、稳定排序均保留。新writer保留空白金额格式、历史Excel日期序列、CR/普通XML文本与公式转义；只在本出口使用兼容Worksheet，禁用自动公式和超链接。
- 型号排名先对完整集聚合与排序，再仅为双榜和当前页PN补价格分位数、池归属与金额展示。既有part_id索引已支持查询，不增加索引。四种实际查询响应逐字段完全一致，默认6.2551→2.1063秒；独立复核384组排序/权限/空值/舍入场景一致。
- 目标HTTP处理器均为同步def，由FastAPI线程池执行；未发现这些路径在事件循环直接运行同步SQL。重复全量PN查询与Python金额转换会放大并发争用。保留10+20连接上限，不用扩池掩盖热点；池等待改为5秒，耗尽返回中文JSON 503。
- `get_db`每个根事务开始时设置 `SET LOCAL statement_timeout='30000ms'`，请求内commit/rollback后重开事务继续生效，SAVEPOINT保留服务自行设置的短预算。SQLSTATE 57014先rollback再返回中文JSON 504，其他数据库异常仍抛出。身份上下文不再吞数据库异常伪装匿名403；无效凭证的原行为保留。后台显式Session不继承请求预算。
- 30秒是**单SQL/锁等待预算**，不保证CPU渲染或多个SQL组成的请求总时长不超过30秒。XLSX完整构建成功后才发送响应头，构建期超时可以返回JSON，随后分块发送并释放数据库连接。
- 可选P3已按实际 `/api/profit?dimension=salesperson|customer` 重测；p50为0.5202/0.7704秒，未改其查询或日期默认值。

## 测量

审计参考：导出94.92秒/13,016,626字节，排名p50=6.18秒；更极端的parts/search 87秒及仪表盘60秒超时是原审计观测。本次同脚本基线没有复现全部极端超时，不能宣称已证明所有原审计长尾来自同一原因。

使用 `scripts/measure_e2e_performance.py`，相同副本、鉴权和六个端点；每端点先串行3次，再12并发执行各4次（共24请求），混合请求60秒read timeout；单独测无筛选导出时为180秒read timeout。每端点混合样本数仅4，表中p95等于该端点最大值；数据用于同机对比，不代表生产SLA。报告包含全部脱敏耗时样本，不含token或响应业务内容。原镜像关键代码和依赖锁与54efe9d基线逐字节相同。

| 指标 | BEFORE | AFTER |
|---|---:|---:|
| 无筛选导出（秒，HTTP 200） | 121.3277 | 41.7492 |
| XLSX字节数 | 13,016,627 | 12,575,849 |
| 型号排名串行p50（秒） | 6.8813 | 1.9065 |

12并发混合请求耗时（秒）：

| 端点 | BEFORE p50 | p95 | max | AFTER p50 | p95 | max |
|---|---:|---:|---:|---:|---:|---:|
| overall | 2.8302 | 25.7250 | 27.7988 | 1.8733 | 4.3545 | 4.3671 |
| parts_search | 2.1884 | 2.3047 | 2.3047 | 1.0112 | 1.7801 | 1.7801 |
| data_quality_issues | 0.2141 | 0.8119 | 0.8119 | 0.2104 | 0.6695 | 0.6695 |
| sales_analytics_orders | 2.8392 | 3.1514 | 3.1514 | 1.9074 | 2.0080 | 2.0080 |
| dashboard_sales | 3.6985 | 4.0835 | 4.0835 | 2.6269 | 3.1947 | 3.1947 |
| dashboard_purchase_orders | 2.7176 | 3.1398 | 3.1398 | 1.7563 | 1.9367 | 1.9367 |
| dashboard_part_ranking | 23.8886 | 27.7988 | 27.7988 | 4.0386 | 4.3671 | 4.3671 |

BEFORE / AFTER 的HTTP 503、504、客户端超时及其他错误均为0。最终AFTER在本任务pytest结束后运行，脚本 `--max-p95 10` 检查exit 0。全量导出仍需约42秒；保留原全量契约，未默认缩窗或截断数据。

复现命令（令牌文件只在本机读取）：

```bash
python3 scripts/measure_e2e_performance.py \
  --token-file /home/cloudlay/workspaces/reviews/e2e-fix-20261019/private/itsp_admin.token \
  --parallel 12 --rounds 4 --timeout 60 --export \
  --output /tmp/itsp-perf-measurement.json
```

原始报告：[BEFORE](../performance/e2e-perf-before.json)、[AFTER](../performance/e2e-perf-after.json)、[真实销售维度基线](../performance/e2e-profit-before.json)。

## 验证

最终相关后端回归：**347 passed，294.80秒，exit 0**。覆盖31个文件，conftest从空库执行Alembic到head；原有5837条弃用warning（主要为既有工作簿样式copy）不影响通过。无前端改动，按任务要求未跑前端构建。

完整复现范围：

```bash
cd backend
PYTEST_DATABASE_BASE_URL=postgresql+psycopg://spareparts:spareparts@127.0.0.1:32864/spareparts_test \
  uv run --extra dev pytest -q \
    tests/test_auth_fallback.py \
    tests/test_auth_lockout.py \
    tests/test_boss_v2_masking.py \
    tests/test_dashboard_kpi.py \
    tests/test_dashboard_masking.py \
    tests/test_dashboard_order_filters.py \
    tests/test_dashboard_order_parts.py \
    tests/test_dashboard_order_safety.py \
    tests/test_dashboard_orders.py \
    tests/test_dashboard_price_discipline.py \
    tests/test_dashboard_ranking.py \
    tests/test_dashboard_ranking_performance.py \
    tests/test_dashboard_trend.py \
    tests/test_data_quality_amount_mismatch.py \
    tests/test_data_quality_amount_mismatch_cli.py \
    tests/test_data_quality_calibration.py \
    tests/test_data_quality_foundation.py \
    tests/test_data_quality_migration.py \
    tests/test_financial_identity_rounding.py \
    tests/test_maintenance_demand_export_ordering.py \
    tests/test_maintenance_download_object_semantics.py \
    tests/test_maintenance_order_export.py \
    tests/test_pool_dashboard_archived.py \
    tests/test_profit_part_id.py \
    tests/test_profit_tax_basis.py \
    tests/test_rbac.py \
    tests/test_request_db_timeout.py \
    tests/test_sales_analytics_api.py \
    tests/test_search_fuzzy.py \
    tests/test_unified_search.py \
    tests/test_field_visibility_preparation.py
```

另已检查 `git diff --check`、脚本编译及依赖锁；CI固定uv 0.11.31可读取更新后的锁文件。

独立复核发现的新writer历史日期偏移和CR文本差异均已修正。额外独立核验10个历史日期序列、529个XML/Unicode/公式边界字符串；常规与read-only读取一致且无公式、富文本或超链接节点。原有39个导出测试函数全部保留。最后增量另独立验证48组权限组合（覆盖221个保护键）、480次并发closure调用和400个类型单元格；与原权限策略和通用writer的值/类型/格式等价。XlsxWriter私有兼容钩子受锁文件和保真测试约束，升级依赖必须重跑这些测试。

测试使用本任务独立PostgreSQL15，未访问共享5433。第一台overlayfs临时库遇到checkpoint清理等待，改用tmpfs `itsp-perf-pytest-20261019-02`（localhost:32863）；最后增量使用同配置 `itsp-perf-pytest-20261019-03`（localhost:32864），关闭fsync/synchronous_commit/full_page_writes仅用于一次性合成测试。最终测试后仅删除本任务临时容器。

## e2e候选与恢复原应用

仅替换 `itsp-e2e-app`，前端仍为 `itsp-e2e-frontend:eede6dd`。数据库、egress和前端容器未重启，数据未做迁移/清理。原compose文件未修改；候选通过Docker创建，不使用compose重建原容器。

- 候选容器：`itsp-e2e-app`，ID `273a16369d9485d2937d7afb9ec36ec5ff591d59649ff0168e24901b5c4fa46f`。
- 候选镜像：`itsp-e2e-backend:perf-wip-20261019-v2`，image ID `sha256:90d918f8b5b8a9714c5a10f0757ea2be0f4234a39b9c76c222cef8d98ab54c0f`。
- 中间候选另保留为 `itsp-e2e-app-perf-v1-20261019`（停止、断开网络），ID `a5fb45bae24b89b385373eb790af0a1bfff3f9e244e82c6ea248c2fc239c7c7f`；无需用于回退。
- 原容器保留为 `itsp-e2e-app-pre-perf-20261019`（停止、未删除），ID `5b905f744d4221bbf4ab1fae5b9332f1c3356d89c31875a1db88e1b19d3579cd`。
- 原镜像：`itsp-e2e-backend:eede6dd`，image ID `sha256:7760b5b0267dda6ad34e1b35267ee2579c743242b7d6b7dc60d1783941ffe613`。
- 候选复用原运行环境和挂载，逐项比较确认环境变量未变。保留原IP：`e2e-xsys:172.21.0.3`、`itsp-e2e-int:172.19.0.4`及别名，因此无需重启反代。
- 为避免重新下载相同依赖，e2e构建以原镜像为FROM，复制本worktree的后端和依赖锁，`pip install --require-hashes -r requirements.lock`仅新增锁定XlsxWriter，再安装本地包。原依赖锁已确认等于54efe9d。仓库生产Dockerfile保持原状。

精确回退（只操作这两个应用容器，候选也保留；数据库无需回滚）：

```bash
docker stop itsp-e2e-app
docker network disconnect e2e-xsys itsp-e2e-app
docker network disconnect itsp-e2e-int itsp-e2e-app
docker rename itsp-e2e-app itsp-e2e-app-perf-20261019
docker network connect --ip 172.21.0.3 \
  --alias itsp-e2e-app --alias itsp-e2e-api \
  e2e-xsys itsp-e2e-app-pre-perf-20261019
docker network connect --ip 172.19.0.4 \
  --alias itsp-e2e-app --alias app \
  itsp-e2e-int itsp-e2e-app-pre-perf-20261019
docker rename itsp-e2e-app-pre-perf-20261019 itsp-e2e-app
docker start itsp-e2e-app
```

恢复后按同一鉴权只读API检查 `http://127.0.0.1:18601`。原运行容器已完整保留，恢复不需要更改任何密钥或env文件。main合并仍需要管理员对候选SHA人工APPROVED review；本任务不合并、不部署生产。
