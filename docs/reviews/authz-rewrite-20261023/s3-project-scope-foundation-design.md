
Canonical analysis 的 `all_projects` 延续全业务范围中未挂靠项目的源事实；只有真实 PDP 返回无条件 SQL 计划、且该 Grant scope 为 `all_projects` 时才不加 project FK 集合限制。任何条件计划仍在每个源事实/实际领用/实收查询聚合前用 SQL 子查询过滤，scope 字符串不能抹去计划。受限范围排除未挂靠事实。该约定不新增项目对象身份、合同明细或客户/成本字段授权。

集合产物的 revision 在同一 RR 事务内计算封闭查询族的关系指纹：`maintenance_authorization_relations.PROJECT_RELATION_COLLECTIONS` 明确列出真实项目查询集合。项目与人员挂靠之外，加入来源单挂靠、领用单及行、返还实收、项目合同、回款快照、费用归属的稳定 ID、parent FK、版本和生效/映射状态。SQL 只返回每张表的 SHA256 聚合摘要，不载入金额、数量、描述或凭据正文；九个摘要在同一 SQL statement 取回，运行期间不缓存。

该版本采用共同关系全集，任何相关表变化可能使不相关的旧集合产物一并失效，这是保守失效边界，不宣称最小失效范围。当前关系重算和同资源 Grant 仍是实际授权依据；摘要相等不能单独放行。记录版本可发现合法版本推进的 A→B→A，但不是对未登记关系或绕过版本纪律的任意 ABA 的保证。

真实 PG/PDP 反例分别把已生成分析内容涉及的来源单、领用单或返还记录移至未授权项目，保持 actor epoch、Grant 版本和项目版本不变。修改前三例均错误通过旧来源校验；修复后原样拒绝为 `AUTHZ_ARTIFACT_STALE`，按当前范围重新生成则可读。元数据 SQL 另有敏感列排除检查。`/tmp/itsp-s3-collection-relations-red.log` 为 4 failed；`/tmp/itsp-s3-collection-relations-green.log` 为 24 passed（4 新例、20 分析/看板回归）。该小型测试数据下上下文解析约 9.46 ms，不能作为生产规模性能结论。

随后扩展为 20,000 条领用头 + 20,000 条领用行的真实 PG 数据集。`EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)` 确认两个关系表各扫描 20,000 行、汇总只返回 1 行；planning 0.825 ms、execution 188.933 ms、客户端 192.94 ms。完整 plan 见 `/tmp/itsp-s3-relationship-scale.json`，5 项反例及规模检查通过 `/tmp/itsp-s3-collection-relations-scale.log`。这是本机合成负载证据：当前每次授权视图、正式物化和导出块重验会重新计算，不能当作恒定延迟。后续优化可以按明确 query family 或真实 parent 范围收窄摘要，必须同步证明保存内容的完整集合与重挂关系仍被覆盖；不能跳过当前重验或缓存“未变化”。
