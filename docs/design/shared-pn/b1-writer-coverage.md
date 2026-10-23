# B1 写入覆盖与验证边界

决策：D-20261023-PN。本文记录候选实现，未部署；全部验证使用隔离 PostgreSQL。B1 的共享协议是事务内版本、保护与审计。HTTP 权限切换、跨系统委托、图库迁移及宿主身份重算队列的完成状态以阶段记录为准，不能从下表推断它们已经完成。

## 入口覆盖

| 原入口 | B1 处理 | 事务与身份边界 |
|---|---|---|
| `master_edit.create_part/edit_part`，`/api/parts/master` | 新建、字段/规格编辑写共享版本、逐字段版本与审计。旧编辑快照增加单调版本，检测 A→B→A；分类两级作为同一选择。旧快照缺版本或字段版本落后返回冲突，草稿不部分保存。 | 保留原最终提交 wrapper；`*_in_transaction` 无提交。HTTP 传 `account:{id}`；内部旧脚本没有实名 ID 时明确 unknown。 |
| `batch_normalize.apply_batch` | 继续调用 `master_edit`，自动获得同一版本/审计保护。 | 保留已约定的逐行提交；HTTP 批量应用仍冻结，不误称整批原子。 |
| `loader._upsert_parts` | 已有 PN 先按稳定 ID 锁定；批量 upsert 后按实际差异推进字段版本和审计；不增加逐行提交。 | 与导入事实共用调用方事务；无有效变化不制造修订/审计。 |
| `loader._upsert_aliases` | 别名实际新增推进父 PN identity revision；重复导入无新变化。 | 与同批 PN/事实共事务；拓扑共享锁保证导入期间别名/合并图不会变。 |
| 通用单文件/批量导入、WBDD 专用导入 | 认证入口捕获当前真实账号 ID；job 和 batch 持久化 nullable `actor_user_id`，worker 沿原 ID 记录发起人，执行者独立；保留 batch ID 与有效 SHA256。 | 历史 `uploaded_by/created_by` 不按同名账号回填；旧凭证没有可验证实名来源时明确 unknown。job 恢复从持久 ID 获取发起人，不按当前显示名猜人。 |
| `master_data.classify_backfill` | 锁内重读后分类，UPDATE 同时复核人工来源与两个分类锁；变更后重绑 category_id；取消函数中途 commit。 | 资料、字典、版本、审计随调用方一起提交或回滚。 |
| `backfill_brands/categories/aliases`、`master_data.refresh` | 独立调用也先捕获/锁父资源；组合 refresh 只记录一次批量实际差异。 | 保留 refresh 最终提交；derived 字典 ID/质量分不冒充 PN 语义修订。 |
| `spec_extract.refresh_part_specs/apply_manual_specs/backfill_specs` | 先父后子锁；人工删除规格写持久 tombstone；自动刷新/全量重建不会复活删除键。人工再次赋值显式清除 tombstone，版本继续增长。 | 直接调用默认记录；命令内嵌调用只由外层统一记录，避免重复审计。 |
| `part_rename.rename_part` | 取得拓扑排他锁，改 PN/别名同事务写 identity revision 与审计。 | 新 `rename_part_in_transaction` 不提交；旧 wrapper 保留最终提交。目标已占用仍生成候选，不自动合并。 |
| `merge.merge_parts/unmerge` | 拓扑排他锁；源、目标、受压缩成员统一排序锁定/记录；目标组强制 identity revision。目标人工删除规格不因 merge 回填；人工清空字段的字典外键一并保护；unmerge 不撤销合并后作出的同值人工确认。 | 拆出无提交的 host transaction 函数；历史事实重指仍留宿主。B1 不新增外部共享 merge API。必要重算任务同事务入队仍待后续阶段。 |
| `master_data.review_alias` | 拓扑锁→父资源→别名行锁并重读；旧/新归属父 PN identity revision 同步。 | 无提交版本可组合事实重指；最终 wrapper 一次提交。 |
| `match_candidates.review` | merge 与候选审核共用一笔事务，去掉旧“merge 已提交、候选另提交”的窗口。 | 审核与独立型号确认都先取得同一排他拓扑锁，避免旧候选行/父 PN 不同获取顺序形成反向等待；新的宿主协调器仍需收口统一锁序。本地治理状态继续既有审计。 |
| `governance.set_excluded` | 父 PN 排他锁后重读，避免缓存实体覆盖。 | `is_excluded` 是 itsp 本地治理轴，不冒充共享 availability；沿用本地审计。 |
| `update_quality_scores` | 保留本地派生更新。 | 不影响共享资料版本，不把全目录质量刷新发布成主数据修改。 |
| `scripts/pn_standardize.py`、`pn_eval_*` | 调用上述已纳入版本协议的 rename/merge wrapper，没有另一直写主表路径。 | 这些历史运维脚本尚未成为新框架实名外部入口；执行前必须改用已授权宿主 adapter。B1 不运行、不重新启用脚本；旧 `agent:*` 不伪装成真人。 |

## 锁与可组合性

- 新核心普通资料写只锁目标父 PN，不拿全局 master 锁；不同 PN 的普通写可并行。
- 旧手工入口与旧全量 refresh 的共同 advisory 锁暂保留；这是兼容边界，不能宣称全量维护已无串行瓶颈。
- 拓扑变更采用独立排他 advisory；导入和创建采用共享模式。导入可与普通资料写并行，身份变更须等当批导入结束，避免半批按旧身份、半批按新身份。
- 新建使用独立 creation gate：人工创建排他，ETL 共享，并只对待插入 PN 取得排序的规范键锁；普通 profile 编辑不取得该 gate。长导入会延迟人工新建，需要按实际业务量评估。
- pipeline 顺序为既有 DATA_CHANGE 锁→共享 topology→宿主项目/工作簿→creation gate/待新建键→PN→事实；未来宿主 merge 重算协调器须遵守同一顺序。禁止持有 PN 后再反向申请宿主 DATA_CHANGE。
- classify/refresh 当前按全部已有 PN 一次排序加锁，真实批量 SELECT 与批量审计避免逐 PN 往返；它仍会阻挡该次扫描范围的人工编辑。B1 有 bounded-query 测试，**未进行生产规模吞吐基准**，后续若切分批次必须另定义事务/水位语义。

## 验证证据与尚未完成

新增 `test_pn_catalog_legacy_writers.py` 验证 ABA、混合草稿原子拒绝、规格删除/重建防复活、Excel 业务/版本/审计整体回滚、别名独立 identity 修订、分类/字典事务回滚、真实两个连接下新核心人工写与旧分类任务等待后重检、合并闭包版本与整笔回滚、别名重指回滚。

`test_import_platform.py` 增加账号 ID、文件 SHA256、job 持久发起人、历史同名账号不自动认领的验证。旧 HTTP 的“旧基线提交同值直接接受”测试按新单调版本契约改为 409，并验证刷新后可明确确认；真正不同字段并发合并测试仍保留。

同时修复批量导入完整性异常分支引用未绑定 `exc` 导致失败原因丢失的问题，实际 worker 用例先红后绿，固定公开错误文案、详细原因写服务端日志。

开发机已有运行证据：上述 writer/HTTP/import/loader/merge/rename 六个测试文件共 **78 passed**；完整回归与最终精确提交证据由主阶段记录补充。没有生产写入、没有线上验收、没有声称 B2–B4 完成。

仍需后续阶段：按新权限框架全面替换旧 HTTP 门禁；可靠跨系统委托和 shop 适配；宿主 merge/unmerge/reassign 的权限交集、预览版本和持久重算任务；PN×grade 内容、图库/OSS fencing、迁移核对与两端验收。B1 只提供这些工作的共享事务基础，不以入口包了一层当作全部完成。
