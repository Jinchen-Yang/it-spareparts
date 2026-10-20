# 商城采购参考均价开发交接

- 分支：`feat/price-reference-purchase`，基线 `origin/main` / `319e25f6cd2b2c0119a0e865893a38a6cf9ca362`；开工 fetch 后远端 main 未变化。
- 决策: D-43。见 [采购均价决策](../decisions/0021-商城参考价采购均价.md)；更正 D-42 的“不返回采购价”。
- 状态：本地实现与开发验证完成，仅本分支提交；未 push、未建 PR、未合并、未部署。未连接生产，未更改 `~/.codex/config.toml`。无迁移、无新增配置、无前端修改。

## 实现

既有参考价 GET 在 sale 的含税/未税两条之后追加 purchase 的含税/未税两条。两域共用窗口、半衰期、加权及过滤配置；采购不适用 `counts_revenue`，只计单头批次行，以 `purchase` 域排除已确认源数据错误。

税率和行级名义侧直接复用采购分析表达式。采购按日期、有效税率与原始税基分组，以同一 Decimal 加权函数换算，最终才 HALF_UP 两位；坏税率或金额超限导致整个请求固定 503。采购日期沿用 `latest_sale_date` / `oldest_sale_date` 字段名；顶层 `calculation` 仍仅描述销售样本。ReferencePrice、顶层与 calculation 均未新增字段。

## 验证证据

测试仅使用 `priceref-itsp-pg-20261019` 的 localhost 32880；`conftest` 每个进程另建独占数据库、迁移到 head，结束清理。以下命令均在 `backend/` 执行，并设置本地测试基址：

```bash
export PYTEST_DATABASE_BASE_URL='postgresql+psycopg://spareparts:spareparts@127.0.0.1:32880/spareparts_test'

uv run --extra dev pytest tests/test_resale_price_reference.py -q --tb=short

uv run --extra dev pytest -q \
  tests/test_purchase_analysis.py \
  tests/test_purchase_analytics_amounts_handcalc.py \
  tests/test_purchase_analytics_equivalence.py \
  tests/test_purchase_analytics_api.py \
  tests/test_purchase_analytics_permissions_migration.py

uvx ruff check \
  app/services/resale_price_reference.py \
  app/schemas/resale_price_reference.py \
  tests/test_resale_price_reference.py \
  tests/resale_price_reference_shop_contract.py
```

- 参考价最终整文件：**75 passed**，115.83 秒，退出码 0。
- 采购分析 5 个文件：**168 passed**，362.56 秒，退出码 0。
- 两次测试均仅一条既有 Starlette/httpx TestClient 弃用提示。
- Ruff 0.17.1：四个变更 Python 文件全部通过；当前 CI 没有独立 Ruff 步骤，此处为额外静态检查。`git diff --check` 通过。
- 先验红灯：旧实现对加入采购种子的契约夹具失败，唯一差异是缺少采购条目；实现后同一夹具通过。
- 覆盖窗口边界、双权重衰减手算、15 种税率/含税标记组合、分侧名义计数、单头金额缺口不影响行级规则、双向最终舍入、旧批次残留、状态/价格/数量/DQ 过滤、空态、隐私、固定查询数和只读请求。
- 冻结商城 DTO 独立于生产者 schema，验证空态、仅销售、仅采购、混合响应，以及每层 `extra=forbid`。AST 核对四个类与本地商城 DTO 相同；生产者 DTO 字段名及顺序与基线一致；文档示例 JSON 与跨库夹具完全一致。
- 独立代理只读审查无阻塞项；额外对新旧销售加权函数进行了 600 次确定性随机对拍，Decimal 结果相同。为同模型不同角色审查，不宣称异构审查。

## 验证边界与后续

金额上限 HTTP 测试在采购聚合边界注入大额：源 `Money(14,2)` 最多 12 位整数，无法通过真实源行达到商城 `1e14` 上限；后续税基换算、加权、上限校验及 503 响应仍走真实实现。数据库失败用异常注入，不代表真实生产故障演练。

无未决业务口径。未运行整个后端全量套件或线上商城端到端验收；本次证据为隔离 PostgreSQL API 回归及商城等价 DTO 兼容校验。后续上线建议 itsp 先于商城或同窗，商城仍按 `users.can_view_purchase` 过滤采购条目；main 合并与部署仍遵循仓库审批/发布规则。
