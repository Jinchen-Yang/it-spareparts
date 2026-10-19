# 日期输入与父实体读取校验交接

- 基线：`origin/main` / `54efe9d55be2e908ce2a2f0182d1e4c57df8afad`。
- 分支：`fix/e2e-input-validation`；仅在独立 worktree 修改，PR 指向 main。
- 决策: 无。修复 HTTP 输入与资源存在性校验，不改变经营统计口径。

## 行为与边界

1. 全局备件行级表导出将 `from/to` 声明为日期，非法或空日期由 FastAPI 返回带参数位置的 422。
2. `api/date_validation.py` 统一拒绝逆序范围；覆盖全局行级表、维保分析三入口、维保明细，以及 dashboard 八个日期查询。相等日期合法；原有可选单边范围继续有效。即使选择日期预设，显式传入的逆序范围也不再被忽略。
3. boss-board projects/summary 按现有默认日期补齐单边输入后校验；summary 对无法计算环比期间的过早或过大范围返回 422。未注册全局 `ValueError` 处理器，其他程序错误仍可被发现。
4. 项目经理工作簿月份解析捕获真实日历错误，`0000-01` 返回明确 422；空月份仍采用原有默认月份。
5. 项目共用 scope guard 先查父项目，再检查行权限。父项目缺失返回 404；存在但无权仍为 403。费用行、回款计划、验收清单/模板、采购记录等在父项目存在但无子行时保留原有 200 空响应。稳定项目 ID 不限 UUID，停用项目仍视为存在。
6. 型号编辑历史先鉴权，再验证整数 ID、PostgreSQL 整数边界和父型号存在性。非法或缺失 ID 返回 404，存在但无人工历史继续返回空列表；合并后的型号仍可读取历史。
7. 验收清单两个 GET 补齐现有页面契约要求的登录/页面权限检查，未登录为 401、无页面权限为 403，再执行父项目检查。共享项目 guard 也使缺失父项目的写请求在写入前返回 404。

## 审计范围

采购分析、销售分析和其他维保工作簿导出已通过专用校验异常返回 422，保留既有实现。`/maintenance/lines` 原先会对逆序范围返回空结果，本次接入共享校验并保留单边范围。无迁移、前端、依赖或 secrets 改动。

## 验证

- 原始两个请求通过真实 HTTP 测试先复现 500，再验证 422。
- 新增测试共 179 项：`test_read_input_validation.py` 86、`test_dashboard_date_validation.py` 32、`test_maintenance_report_month_validation.py` 8、`test_parent_resource_reads.py` 53，均已通过。
- 207 个相关文件的最终完整回归：**3336 passed、2 skipped、0 failed**。三组分别为 1151 passed/1 skipped、1014 passed、1171 passed/1 skipped，退出码均为 0。两个原有跳过项分别是需显式启用的生产规模成本重算基准、未配置外部真实样例的只读验收。
- 两条旧工作簿缺失项目 403 断言按本任务更新为 404；已有项目越权 403 测试保留。费用归集工作簿单文件另复跑 49 passed，所属完整分组也已重跑通过。
- 覆盖非法/空/无效日历日期、逆序、同日、默认日期、单边范围、自定义缺边、环比下溢、非法 ID、缺失 UUID、存在但无子行、历史字符串 ID、停用/合并实体，以及 401/403/404 边界。
- 独立代理静态审查未发现阻塞问题；同模型角色审查，不宣称异构模型审查。
- 迁移链单 head：`c9d4f2a6b8e1`。pytest 每进程新建独占 PostgreSQL 数据库并运行迁移；相关完整回归使用任务专用 PostgreSQL 15 临时容器、合成数据和内存盘，不验证崩溃持久性。

相关完整回归按下列选择器覆盖 207 个文件；本次将排序后的列表轮流分为三组并行运行，各进程独占数据库。可在 `backend/` 中按相同范围顺序复跑：

```bash
uv run --extra dev python - <<'PY'
from pathlib import Path
import subprocess
import sys

prefixes = (
    'test_maintenance', 'test_dashboard', 'test_master_edit', 'test_pool',
    'test_site_issue', 'test_manual_site_issue', 'test_part', 'test_acceptance',
    'test_scope', 'test_purchase_analytic', 'test_sales_analytic',
    'test_read_input_validation', 'test_parent_resource_reads',
)
files = sorted(str(p) for p in Path('tests').glob('test_*.py')
               if p.name.startswith(prefixes))
subprocess.run([sys.executable, '-m', 'pytest', '-q', *files], check=True)
PY
```

## 交付限制

本记录仅代表开发验证，不代表生产验收、合并或部署。未触碰 main 工作树或生产。main 合并仍须管理员对当前候选 SHA 的人工 GitHub APPROVED review。
