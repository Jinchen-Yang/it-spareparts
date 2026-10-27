# M1 实时型号检索契约

调用链：`LivePartSearch` → `GET /parts/suggest` → `part_search.search` → PostgreSQL 检索投影。
旧 `/parts/search` 与 `PartPicker` 继续使用原契约。

## 服务

`search(db, q, *, limit=10, filters=None) -> list[dict]` 不提交事务。空查询不访问数据库，最多返回 30 条。
查询和投影共用 NFKC、大写、合并空白口径；紧凑形式仅保留 ASCII A–Z 和 0–9，与现有 `pn_compact` 字符集合一致。
每个空白分词均须在资料文档或紧凑文档命中。`*` 匹配任意串，`?` 匹配单字符；`%`、`_`、反斜杠按字面匹配，含这三种符号的词不走紧凑召回。

归一化后为 1–2 个字符的纯 ASCII 字母数字词或纯汉字词，使用独立窄表 `part_search_short_key.keys @> ARRAY[词]`（GIN `array_ops`）召回。每个键本身就是同一归一化 `doc` 的字面子串，因此不再冗余读取正文做 LIKE；仍关联文档行以保留文档存在条件，纯短词可利用文档主键索引读取。生成器契约测试同时守护「每个键必为规范化原文子串」。ASCII 短词按 **token 前缀**匹配：`S` 命中 `SAS`、`X-SAS`，不命中 `ABS`；`M1-12345` 分成 `M1`、`12345`，其键为 `M`、`M1`、`1`、`12`。短词不跨 token 拼接，因此 `AB` 不命中 `A-B`。这是对原任意子串/紧凑子串语义的有意收窄；从 3 字符起沿用原 trigram 和紧凑路径。
汉字短词仍是子串语义：连续串「机械硬盘」生成「机」「械」「硬」「盘」「机械」「械硬」「硬盘」，标点或 ASCII 切断连续串。汉字范围为 U+3400–4DBF、U+4E00–9FFF、U+F900–FAFF、U+20000–2FA1F、U+30000–323AF，生成器与查询端保持一致。全角输入先按既有 NFKC 规则归一化。含通配符、字面符号、ASCII/汉字混合或其他字符的短词保留旧路径，避免无对应短词键时漏召回。多词查询逐词选择车道，其他长词的容错不能绕过短词条件。

直接结果按 PN/生效别名精确、PN 前缀、PN 子串、报价钩子、字段权重分档；字段权重为品牌/制造商 3、品类/规格 2、其他资料 1，多词相加。同档按 PN 排序。
分档和字段权重计算之前，资料候选按 PN 稳定取前 N 条，环境变量 `PART_SEARCH_CANDIDATE_LIMIT` 可配置 N（默认 300，至少 1）。PN/生效别名精确、PN 前缀、PN 子串及报价加权分别保留最多 `limit` 条候选，再与资料候选去重、统一排序；精确 PN 和别名不会被资料上限挤出。有限资料候选不承诺全库字段权重最优。
不足时用事务级 pg_trgm 阈值 0.25/0.35 补足，逐词要求直接命中或相似。显式通配、字面符号和少于 3 字符的词保持直接匹配，其余词仍可纠错。
模糊召回沿用 resolver 的候选池思路：先 PN/别名词相似，再补整体相似，每条车道最多保留 60 个候选；不足时再用最多 120 个资料候选补足。已有 PN/别名纠错结果时，额外资料的词相似阈值提高为 0.6，避免通用片段产生一屏弱相关结果；纯资料纠错仍使用 0.35。所有已召回的模糊结果统一按相似度和 PN 排序，带 `match_type=fuzzy`。有限候选不承诺全库相似度最优。
投影全文与查询均已转大写，SQL 使用等价的 LIKE，避免 ILIKE 重复执行全文大小写折叠。单字符和纯 ASCII 两字母查询在精确及 PN 前缀档满额时提前停止，其排序与完整分档查询一致。短词的这两档先按 PN 找行，再以相关子查询核对投影，避免为十条前缀物化全部高频键。
短词查询的 PN 各车道先分别保留 `limit` 条，资料先按 PN 保留 `candidate_limit` 条，再合并打分，避免展开全部高频文档。含数字的短词（如 `1T`、`12`）在短键召回后复用原来的 `matched_parts` 候选骨架，只对命中的型号分档，避免 PN 子串车道额外扫描全库。长度 ≥3、通配及错拼沿用基线 SQL、候选车道和规划设置；混合查询也只替换其中短词自身的召回条件。字段权重和最终排序不变。所有查询仅沿用基线 `SET LOCAL jit=off`，不修改调用方的 `plan_cache_mode` 或 `max_parallel_workers_per_gather`，短词调用不会影响后续长词。短词自身的模式/短键参数通过 SQLAlchemy `literal_execute` 安全编译为常量，避免 prepared generic plan 看不到实际词频；只有整个查询是单短词时，才同时编译其 PN 派生参数和候选上限，保证提前停止的估计。混合查询的长词和整查询 PN 参数保持原绑定方式。
非 ASCII PN 与别名的归一化回退分别使用 `ix_part_search_non_ascii`、`ix_alias_search_non_ascii` 两枚 partial Btree。ASCII 精确 PN 继续使用已有 compact 索引，三种精确证据独立召回后合并，保留全角和其他 Unicode 写法。仅纯汉字短词先排除不可能归一化成汉字的 ASCII PN/别名；长词保留基线条件。
窄表只在查询含短词时通过 `JOIN part_search_short_key k USING (part_id)` 关联。30000 型号、同库交替三对且各30次的 JOIN/IN 对照，短词 S、硬盘、HP、1T 和混合词的中位 p95（IN→JOIN，ms）为 28.013→21.853、36.913→19.088、14.024→9.715、56.979→55.229、62.948→50.955；M 为 28.483→32.002、内存为 37.185→37.598，选择总体较快的 JOIN。两个 partial Btree 分别独立在同库事务性删除/回滚后比较：PN 非 ASCII 回退 execution（无索引→有索引）为 12.342→0.014ms、buffers 234→2；别名为 7.260→0.009ms、buffers 518→2，均保留。该扫描对照隔离的是回退分支，不替代完整服务基准。
完整21类同序采样的四种索引组合对照也支持保留：删除 PN 索引后，硬盘/内存短词 p50 从9.428/10.187升至65.764/67.372ms；删除别名索引后，品牌 p50 从15.862升至23.602ms。移除短词冗余正文 LIKE 后，1T 的实际计划改用文档主键 Index Only Scan（Heap Fetches=0），根 buffers 从9148降至5242。四组合诊断包含计时后的prepared/EXPLAIN探测，会影响随后计划计数与缓存，不单独用于判定索引因果。以上对照用于解释选择，正式验收仍看完整三对基准。
纯 ASCII 短词的 PN 紧凑子串分档使用等价的 `strpos`，避免 `%1T%` 一类模式在旧 `pn_compact` GIN 上遍历整个索引；Unicode PN 回退仍用原 ILIKE，长词仍使用原来的 trigram 条件。文档召回的 ASCII token 前缀限制继续生效。

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
`part_search_text` 始终只含基线的 `part_id`、`doc`、`doc_compact`、`updated_at` 四列。短键存入独立的 `part_search_short_key(part_id integer PRIMARY KEY REFERENCES dim_part(id) ON DELETE CASCADE, keys text[] NOT NULL)`；`ix_part_search_short_key_keys` 是 `keys` 上的 GIN `array_ops` 索引。两张投影由同一 `refresh_part_search_text` 函数维护，从同一份归一化 `doc` 生成短键、去重保存，一起提交或回滚；墓碑同步删除两张投影，不需要异步任务。新迁移 `d6f8a0b2c4e6_part_search_short_keys` 只向窄表回填派生键，不 ALTER/UPDATE 原文档表，原列定义、数据、更新时间和 heap 页数保持基线；不改已上线的 `c4e6a8b1d3f5`。手工重建仍调用同一刷新函数，因此也会同时修复两张投影。
规格标签小表 `spec_key_label` 是 `SPEC_KEYS` 的冻结副本，测试约束两端完全一致；新增规格键时应同步新增迁移。

迁移执行全量回填。手工重建：

```bash
cd backend
DATABASE_URL=<目标数据库> uv run --no-sync python scripts/rebuild_part_search_text.py --dry-run
DATABASE_URL=<目标数据库> uv run --no-sync python scripts/rebuild_part_search_text.py
```

基准脚本 `scripts/bench_part_suggest.py` 仅接受本机测试库基础地址，自动创建并销毁独立数据库；默认 30000 型号（每型号两条规格和一个生效别名），批量造数后 VACUUM ANALYZE 合并 GIN pending list。合成数据约 30% 带「硬盘」、20% 带「内存」，PN 首段约 50% 为 M1、30% 为 S、10% 为 HP、10% 为 D。保留原 15 类，并独立增加 S、M、硬盘、内存、HP、1T 六类，每类默认 30 次；逐类断言召回语义、输出完整服务耗时 p50/p95 与达标标记，并输出真实 SQL 及原始 ILIKE/LIKE 的 EXPLAIN (ANALYZE, BUFFERS)。默认每类本地 p95 均须 ≤70ms，为较慢的生产 CPU 留余量，不以总体分位数替代。不会关闭顺序扫描来强制索引计划。

脚本先在旧投影 revision 造数，再升级到 `--revision`（默认 `head`），记录完整迁移及窄表 INSERT 回填耗时。`--output` 保存 JSON；单轮对照旧版时用 `--revision c4e6a8b1d3f5 --baseline-service-source <本地旧服务源码快照>`。`--compare-r5 --baseline-service-source <本地旧服务源码快照>` 在同一个独占库造数一次，实际升降级切换 c4/r5 结构，按基线→r5 连跑三对，每类每半轮30次。每半轮重建连接，保留原始样本、SQL、计划、列定义、文档指纹和 heap 页数。逐类比较三个 p95 的中位数，r5 须 ≤ `max(基线中位p95 × 1.15, 基线中位p95 + 8ms)`，六类短词另须 ≤70ms；任一失败返回非零。`--target-p95-ms` 只调整单轮测量的退出阈值，不改变三对比较的门槛。

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

短词迁移 `d6f8a0b2c4e6` 在同一事务内显式锁住 `dim_part`、`product_specs`、`part_alias`（`ShareRowExclusiveLock`），再建窄表、替换刷新函数、回填、建 GIN 和两枚 partial Btree。源表写入暂停到整条迁移命令提交，避免旧刷新函数与回填交错导致漏键；普通 SELECT 兼容这些表锁，原 `part_search_text` 仅回填读取取得 `AccessShareLock`，不再取得原加列方案的 `AccessExclusiveLock`。新窄表提交前对其他会话不可见，必须先迁移成功再切新代码。仍须安排写入维护窗口；`lock_timeout='5s'` 不是迁移耗时上限。降级也锁住三张源表，先切回旧检索代码，再恢复旧刷新函数并删除窄表、两个 partial 索引及短键生成器，原 trigram 投影与源表触发器继续可用。

此 revision 尚未上线，直接修订为窄表版本；如果本地测试库曾应用过 r3/r4 的同号加列版本，必须先用旧迁移降级到 c4，再应用新版，不能在已登记同号 revision 的库上仅执行 `upgrade head`。本轮基准均使用全新隔离库，生产上线以前置 c4 为基线。

本机 PostgreSQL 15 测试库实测（30000 型号、60000 规格、30000 生效别名）：窄表迁移总耗时 **5975.919ms**，其中窄表 INSERT 回填 **3245.777ms**、GIN 建索引 **2617.135ms**；从三源表显式加锁返回至迁移命令返回约 **5913.786ms**。回填后的 `pg_locks` 确认三源表持有 `ShareRowExclusiveLock`，原文档表只持有 `AccessShareLock`；新窄表持有建表和索引相关锁。原 `part_search_text` 为 **4155 heap 页**，新窄表为2460页。源表写入暂停窗口应覆盖约6秒整次事务并为生产磁盘、锁等待、字段长度和同批迁移留余量，不能把回填 SQL 结束当作解锁。

当前本地完整三对测量中，原文档列定义、数据指纹、relfilenode 和4155页 heap 在六半轮完全相同，六类短词中位 p95 均≤70ms；但品牌、错拼、纯数字仍超相对阈值（分别24.623→42.361、53.452→113.967、57.814→112.147ms），**性能验收尚未通过**。原文档物理宽度已恢复，不能把这次改造当作所有长词回退已解决；上线前仍须处理这些失败项并重新验收。

## 前端复用

`LivePartSearch` 的 `minChars` 默认 1，以 trim 后的 Unicode codepoint 数判断输入门槛。普通输入为 1 字符时防抖 300ms，2 字符及以上为 150ms；IME 组合结束沿用立即检索，但仍受 `minChars` 限制。支持 AbortController、请求代次守卫、键盘选择、单选/多选和原文添加，保留两行候选布局与缩略图。
`renderThumb`、`renderBadges` 为 M2/M3 留空插槽，默认不渲染。
默认无激活候选，Enter 保留页面原搜索提交；上下键激活候选后 Enter 选择。搜索页选择已知候选后沿用 `part_id` 深链打开全景，同时按该 PN 发起原精确搜索并清空结构化筛选。
