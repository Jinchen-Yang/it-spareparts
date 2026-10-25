# M1 实时型号检索契约

调用链：`LivePartSearch` → `GET /parts/suggest` → `part_search.search` → PostgreSQL 检索投影。
旧 `/parts/search` 与 `PartPicker` 继续使用原契约。

## 服务

`search(db, q, *, limit=10, filters=None) -> list[dict]` 不提交事务。空查询不访问数据库，最多返回 30 条。
查询和投影共用 NFKC、大写、合并空白口径；紧凑形式仅保留 ASCII A–Z 和 0–9，与现有 `pn_compact` 字符集合一致。
每个空白分词均须在资料文档或紧凑文档命中。`*` 匹配任意串，`?` 匹配单字符；`%`、`_`、反斜杠按字面匹配，含这三种符号的词不走紧凑召回。

直接结果按 PN/生效别名精确、PN 前缀、PN 子串、报价钩子、字段权重分档；字段权重为品牌/制造商 3、品类/规格 2、其他资料 1，多词相加。同档按 PN 排序。
分档和字段权重计算之前，资料候选按 PN 稳定取前 N 条，环境变量 `PART_SEARCH_CANDIDATE_LIMIT` 可配置 N（默认 300，至少 1）。PN/生效别名精确、PN 前缀、PN 子串及报价加权分别保留最多 `limit` 条候选，再与资料候选去重、统一排序；精确 PN 和别名不会被资料上限挤出。有限资料候选不承诺全库字段权重最优。
不足时用事务级 pg_trgm 阈值 0.25/0.35 补足，逐词要求直接命中或相似。显式通配、字面符号和少于 3 字符的词保持直接匹配，其余词仍可纠错。
模糊召回沿用 resolver 的候选池思路：先 PN/别名词相似，再补整体相似，每条车道最多保留 60 个候选；不足时再用最多 120 个资料候选补足。已有 PN/别名纠错结果时，额外资料的词相似阈值提高为 0.6，避免通用片段产生一屏弱相关结果；纯资料纠错仍使用 0.35。所有已召回的模糊结果统一按相似度和 PN 排序，带 `match_type=fuzzy`。有限候选不承诺全库相似度最优。
投影全文与查询均已转大写，SQL 使用等价的 LIKE，避免 ILIKE 重复执行全文大小写折叠。单字符查询在精确及 PN 前缀档满额时提前停止，其排序与完整分档查询一致。

筛选支持 `brand`、`category_major`、`category_minor`、`part_ids`，未知筛选报错。
`filters['boost_part_ids']` 支持 ID 集合或 `(db, normalized_q) -> set[int]` 回调；默认钩子返回空集合，不读取任何报价业务表。

返回字段：`part_id`、`pn_std`、`brand`、`manufacturer`、`summary`、`description`、`category_major`、`category_minor`、最多三条 `{label, value}` 关键规格、`match_type`、中文 `match_reason`、归一化后的 `matched_terms`。
关键规格优先展示本次命中的规格。接口沿用登录鉴权、`apply_field_visibility` 和访问审计（action=`suggest`）；调用方须容忍被权限过滤的字段缺省。
当前动态库存批量服务需要四次 SQL，没有单次查询的批量库存出口，本轮不返回 `stock_qty`。

## 投影生命周期

`dim_part`、`product_specs`、`part_alias` 的 INSERT/DELETE 在同事务触发刷新。UPDATE 只监听投影依赖列，并用 `IS NOT DISTINCT FROM` 比较新旧值，同值回写跳过刷新：

- `dim_part`：`pn_std`、`pn_raw_sample`、`brand`、`manufacturer`、`summary`、`description`、`category_major`、`category_minor`、`status`。
- `product_specs`：`part_id`、`spec_key`、`spec_value`。
- `part_alias`：`part_id`、`pn_std`、`pn_raw`、`status`。

`data_quality_score`、字典关联 ID 和时间戳等无关列更新不重建投影。规格单位和中文标签取自冻结词典，不取 `product_specs.spec_unit`。
仅 `status=active` 的别名参与。别名没有 `part_id` 时按 `pn_std` 回退关联；型号改名后重新按当前 PN 关联，旧 PN 的无 ID 别名退出、新 PN 的无 ID 别名进入。搜索触发器不自动改写别名业务行；业务随后修改别名的 `pn_std` 时仍刷新新旧归属。
刷新先锁定父型号，再读取规格/别名，防止并发子表写入覆盖投影。移动归属会刷新新旧双方。`merged` 墓碑直接删除投影，查询也排除墓碑。
规格标签小表 `spec_key_label` 是 `SPEC_KEYS` 的冻结副本，测试约束两端完全一致；新增规格键时应同步新增迁移。

迁移执行全量回填。手工重建：

```bash
cd backend
DATABASE_URL=<目标数据库> uv run --no-sync python scripts/rebuild_part_search_text.py --dry-run
DATABASE_URL=<目标数据库> uv run --no-sync python scripts/rebuild_part_search_text.py
```

基准脚本 `scripts/bench_part_suggest.py` 仅接受本机测试库基础地址，自动创建并销毁独立数据库；默认 30000 型号（含规格和生效别名），批量造数后 VACUUM ANALYZE 合并 GIN pending list。15 类查询各 30 次，含各约 30% 型号命中的「硬盘」「HPE」；逐类断言召回语义、输出完整服务耗时 p50/p95 与达标标记，并输出真实 SQL 的 EXPLAIN ANALYZE。每类 p95 均须 ≤150ms，不以总体分位数替代。不会关闭顺序扫描来强制索引计划。

写入基准 `scripts/bench_part_search_writes.py` 先在前一版结构中生成 30000 型号、每型号两条规格和一条生效别名，再运行真实迁移，采集回填耗时与事务内锁。无关列批量更新使用双模式预热、交替顺序、每次 VACUUM ANALYZE 后重复测量，按中位数比较启用/停用搜索触发器。相关列批量更新期间通过真实 `/api/parts/suggest` 路由在另一数据库会话读取，要求请求开始和结束时写语句都仍在执行。

## 上线注意

`c4e6a8b1d3f5` 在一个事务内创建投影、回填和建立两个 GIN 索引，事务提交才释放锁。`SET LOCAL lock_timeout='5s'` 只限制每次等锁时间，不限制迁移总时长。
以下仅列已处于前置版本 `b2f4a6c8d1e3`、单独应用本迁移的锁。`alembic/env.py` 让整条升级命令共用事务，同批其他 revision 的锁会叠加并保持到整条命令提交；例如同时应用前置版本的 `ALTER TABLE dim_part ADD COLUMN` 会持有 `AccessExclusiveLock`，此时普通 SELECT 也会在回填期间等待，不能套用下表的读兼容结论。

| 对象 | 锁与持有范围 | 对并发操作的影响 |
|---|---|---|
| `dim_part` | 创建投影外键及触发器取得 `ShareRowExclusiveLock`，一直持有到迁移提交；回填还取得 `RowShareLock`，逐行 `FOR NO KEY UPDATE` 锁同样保持到提交 | 所有 INSERT/UPDATE/DELETE、COPY 写入等待；对已锁型号的行锁读取也可能等待 |
| `product_specs`、`part_alias` | 创建对应触发器取得 `ShareRowExclusiveLock`，从创建时持续到迁移提交；回填读取还有 `AccessShareLock` | 所有 INSERT/UPDATE/DELETE、COPY 写入等待；普通 SELECT 可继续 |
| 新增 `part_search_text`、`spec_key_label` | CREATE TABLE 的 `AccessExclusiveLock` 保持到提交；投影回填写入另有 `RowExclusiveLock`，普通 CREATE INDEX 还取得 `ShareLock` | 新结构提交前对其他会话不可见，应在迁移提交后才启用新检索流量 |

本机 PostgreSQL 15.19 测试库实测（30000 型号、60000 规格、30000 生效别名）：回填 `SELECT refresh_part_search_text(id) FROM dim_part ORDER BY id` 为 **2265.785ms**，含建表、回填、两个 GIN 索引及提交的整次迁移为 **3914.926ms**。按各源表 DDL 返回到迁移提交后的命令返回计，`dim_part` 锁持续约 **3814.739ms**，`product_specs` 约 **3719.436ms**，`part_alias` 约 **3719.243ms**；回填后的 `pg_locks` 实测确认三张源表均持有 `ShareRowExclusiveLock`。同规模可先按约 4 秒写入窗口估算，并为生产磁盘、字段长度、锁等待和同批其他迁移另留余量，不能只按约 2.3 秒回填耗时安排窗口。

因此主数据编辑、改名/合并、规格提取、别名审核、数据质量刷新以及涉及这三张表的 ETL 都应避开该窗口。普通 SELECT 与这些源表的表锁兼容，可以继续读取已提交数据；等待写锁的长事务也应在窗口前结束，避免等锁超时。迁移开始后才累计取得的每个锁均在同一提交点释放，不能把单条回填 SQL 的结束当作解锁。

迁移只新增派生结构、函数和触发器，不改写业务表记录。降级删除搜索投影、标签表、函数和触发器，采购/销售/库存等业务数据保留；删除触发器时也需取得源表 DDL 锁，应在维护窗口执行。上线顺序为：暂停相关写入并等待在途事务结束 → 先执行迁移并确认提交 → 检查投影行数和联想接口 → 再切换到新代码流量、恢复写入。回滚时先切回旧接口，再降级移除投影。

## 前端复用

`LivePartSearch` 支持 150ms 防抖、AbortController、请求代次守卫、IME 组合结束立即检索、键盘选择、单选/多选和原文添加。
`renderThumb`、`renderBadges` 为 M2/M3 留空插槽，默认不渲染。
默认无激活候选，Enter 保留页面原搜索提交；上下键激活候选后 Enter 选择。搜索页选择已知候选后沿用 `part_id` 深链打开全景，同时按该 PN 发起原精确搜索并清空结构化筛选。
