# M1 实时型号检索契约

调用链：`LivePartSearch` → `GET /parts/suggest` → `part_search.search` → PostgreSQL 检索投影。
旧 `/parts/search` 与 `PartPicker` 继续使用原契约。

## 服务

`search(db, q, *, limit=10, filters=None) -> list[dict]` 不提交事务。空查询不访问数据库，最多返回 30 条。
查询和投影共用 NFKC、大写、合并空白口径；紧凑形式仅保留 ASCII A–Z 和 0–9，与现有 `pn_compact` 字符集合一致。
每个空白分词均须在资料文档或紧凑文档命中。`*` 匹配任意串，`?` 匹配单字符；`%`、`_`、反斜杠按字面匹配，含这三种符号的词不走紧凑召回。

直接结果按 PN/生效别名精确、PN 前缀、PN 子串、报价钩子、字段权重分档；字段权重为品牌/制造商 3、品类/规格 2、其他资料 1，多词相加。同档按 PN 排序。
不足时用事务级 pg_trgm 阈值 0.25/0.35 补足，逐词要求直接命中或相似。显式通配、字面符号和少于 3 字符的词保持直接匹配，其余词仍可纠错。
模糊召回沿用 resolver 的候选池思路：先 PN/别名词相似，再补整体相似，每条车道最多保留 60 个候选；不足时再用最多 120 个资料候选补足。已有 PN/别名纠错结果时，额外资料的词相似阈值提高为 0.6，避免通用片段产生一屏弱相关结果；纯资料纠错仍使用 0.35。所有已召回的模糊结果统一按相似度和 PN 排序，带 `match_type=fuzzy`。有限候选不承诺全库相似度最优。
投影全文与查询均已转大写，SQL 使用等价的 LIKE，避免 ILIKE 重复执行全文大小写折叠。单字符查询在精确及 PN 前缀档满额时提前停止，其排序与完整分档查询一致。

筛选支持 `brand`、`category_major`、`category_minor`、`part_ids`，未知筛选报错。
`filters['boost_part_ids']` 支持 ID 集合或 `(db, normalized_q) -> set[int]` 回调；默认钩子返回空集合，不读取任何报价业务表。

返回字段：`part_id`、`pn_std`、`brand`、`manufacturer`、`summary`、`description`、`category_major`、`category_minor`、最多三条 `{label, value}` 关键规格、`match_type`、中文 `match_reason`、归一化后的 `matched_terms`。
关键规格优先展示本次命中的规格。接口沿用登录鉴权、`apply_field_visibility` 和访问审计（action=`suggest`）；调用方须容忍被权限过滤的字段缺省。
当前动态库存批量服务需要四次 SQL，没有单次查询的批量库存出口，本轮不返回 `stock_qty`。

## 投影生命周期

`dim_part`、`product_specs`、`part_alias` 的 INSERT/UPDATE/DELETE 在同事务触发刷新；仅 `status=active` 的别名参与。别名没有 `part_id` 时按 `pn_std` 回退关联。
刷新先锁定父型号，再读取规格/别名，防止并发子表写入覆盖投影。移动归属会刷新新旧双方。`merged` 墓碑直接删除投影，查询也排除墓碑。
规格标签小表 `spec_key_label` 是 `SPEC_KEYS` 的冻结副本，测试约束两端完全一致；新增规格键时应同步新增迁移。

迁移执行全量回填。手工重建：

```bash
cd backend
DATABASE_URL=<目标数据库> uv run --no-sync python scripts/rebuild_part_search_text.py --dry-run
DATABASE_URL=<目标数据库> uv run --no-sync python scripts/rebuild_part_search_text.py
```

基准脚本 `scripts/bench_part_suggest.py` 仅接受本机测试库基础地址，自动创建并销毁独立数据库；默认 30000 型号（含规格和生效别名），批量造数后 VACUUM ANALYZE 合并 GIN pending list。13 类查询各 30 次，先断言召回语义，再输出完整服务耗时 p50/p95 及真实 SQL 的 EXPLAIN ANALYZE。不会关闭顺序扫描来强制索引计划。

## 前端复用

`LivePartSearch` 支持 150ms 防抖、AbortController、请求代次守卫、IME 组合结束立即检索、键盘选择、单选/多选和原文添加。
`renderThumb`、`renderBadges` 为 M2/M3 留空插槽，默认不渲染。
默认无激活候选，Enter 保留页面原搜索提交；上下键激活候选后 Enter 选择。搜索页选择已知候选后沿用 `part_id` 深链打开全景，同时按该 PN 发起原精确搜索并清空结构化筛选。
