# S3 文件 ownership 与核心接缝（只读实施准备）

根已指定：root 负责 authz/catalog/policies/service/provenance/单一迁移；backend_surface_inventory 负责关系基础、关系锁与 project/read/finance 94 入口；Astra 依次负责 workbooks/source 64 与 flow/warehouse 81；前端 owner 负责全 S3 UI。此文件不是开工指令。

## 先落共同基础

唯一领域基础 owner：`backend/app/services/maintenance_authorization.py`（新增），并负责 `maintenance_project_assignments.py`、`maintenance_project_catalog.py` 的锁/关系原语改造。既有 `api/maintenance_project_scope.py` 由同 owner 改成兼容 transport adapter，受保护服务不再反向 import API。

- `maintenance_project_operations.py` 由 project/read/finance owner 独占（含共用 state/project 锁 helper）；其他包调用稳定 helper/callback，不并写该文件。
- `maintenance_source_assignments.py` 由 workbooks/source owner 独占；自动挂靠调用基础 owner 提供的最终用户锁集合原语，不在 actor SHARE 后自行追加 target UPDATE。
- `maintenance_site_issue_facts.py`、`maintenance_cost.py`、`maintenance_boss_board.py`、`maintenance_project_export.py` 归 project/read/finance；workbook 包只消费已投影/纯计算接口。
- `maintenance_row_filter_registry.py`、`maintenance_row_workbench.py`、`maintenance_workbook_renderer.py` 归 workbooks/source；project 包只调用其显式字段选择接口。
- `maintenance_return_receipts.py` 服务归 flow/warehouse；project finance 仅调用已选投影的聚合原语，不据此暗授receipt明细。
- `import_safety.py` / 通用文件解析不是权限owner，不加入第二套身份规则。多API共用上传/错误文件/worker清理辅助由workbooks/source统一抽到services，不从其他API import。

## 冻结的最小核心合同

1. 继续 `current_verified_identity -> VerifiedIdentity`，`load_principal(db, identity, locked_user=...)`；新身份不得由旧ctx、username或持久JSON伪造。
2. root保持 `resource_access(db, identity, resource, action, *, bindings, attributes, locked_user, object_id, authorization_revision)`；追加typed `domain_context`时由root统一演进。scope为all_projects同样requires_context；原无context的batch `read_accesses`不放宽给S3。
3. Project query_attributes固定 `request.resource.attr.is_manager/is_viewer/is_sales/is_active`。领域基础把 SQL EXISTS/列比较放进派生子查询的 labelled Boolean 列，QueryBindings 绑定具有 .table 的子查询列；取得 predicate 后用 project_id IN (SELECT project_id FROM 子查询 WHERE predicate)。禁止裸 EXISTS/BinaryExpression 绑定或不相关 FROM。SQL 条件进入聚合/分页/候选查询之前。
4. `ProjectReadPlan`包含ResourceAccess、同一predicate、domain_context；`ProjectObjectAccess`额外证明实际对象命中SQL predicate。只有后者生成对象allow；计划存在本身不能证明任何对象属于scope。
5. `DocumentAccess`按每个实际project择一个完整合同，记录全部对象依赖和格式内operation要求。不得跨resource合并fields。
6. 领域写原语取得原有全部domain锁后调用授权callback，传已按稳定ID及最终强度锁好的用户集合；callback不commit、不加domain锁、不升级用户锁。普通actor SHARE足够，涉及target用户UPDATE一次锁最终强度。
7. root扩展现有provenance schema2，以(resource, closed context kind/id/selection)分别记录，重验时注册的domain validator重新解析真实parent/完整政策；不能仅比较revision。任务恢复使用root可信resume_task_identity(owner_id,enqueued_epoch)。

完整73资源/228 human入口、上下文kind、JSON形状与resolver规则统一见 [s3-execution-spec.json](s3-execution-spec.json)；全字段与ABA约束见 [s3-project-authorization-contract.md](s3-project-authorization-contract.md)。query EXISTS适配须在实施前用真实PDP最小向量证明，当前是冻结输入协议，未声称runtime已验证。

## 精确API文件分组

### project_read_finance

94 入口；owner: backend_surface_inventory。

- `backend/app/api/maintenance.py`
- `backend/app/api/maintenance_analytics.py`
- `backend/app/api/maintenance_audit.py`
- `backend/app/api/maintenance_boss_board.py`
- `backend/app/api/maintenance_collection_evidence.py`
- `backend/app/api/maintenance_collection_plan_imports.py`
- `backend/app/api/maintenance_collection_reminders.py`
- `backend/app/api/maintenance_dashboard.py`
- `backend/app/api/maintenance_expense_reconcile.py`
- `backend/app/api/maintenance_front_stock.py`
- `backend/app/api/maintenance_project_operations.py`
- `backend/app/api/maintenance_projects.py`
- `backend/app/api/maintenance_recovery.py`

### workbooks_source_ownership

64 入口；owner: Astra（先source，后flow）。

- `backend/app/api/maintenance_ai_fallback.py`
- `backend/app/api/maintenance_doc_import.py`
- `backend/app/api/maintenance_expense_collection_workbook.py`
- `backend/app/api/maintenance_ledger.py`
- `backend/app/api/maintenance_manager_directory.py`
- `backend/app/api/maintenance_manager_workbooks.py`
- `backend/app/api/maintenance_migration.py`
- `backend/app/api/maintenance_project_assignments.py`
- `backend/app/api/maintenance_project_batch_transfer.py`
- `backend/app/api/maintenance_project_master_workbook.py`
- `backend/app/api/maintenance_project_workbook_v3.py`
- `backend/app/api/maintenance_project_workbooks.py`
- `backend/app/api/maintenance_row_workbench.py`
- `backend/app/api/maintenance_source_assignments.py`
- `backend/app/api/maintenance_source_cancel_cascade.py`
- `backend/app/api/maintenance_wbdd_import.py`

### flow_warehouse

81 入口；owner: Astra（先source，后flow）。

- `backend/app/api/maintenance_acceptance.py`
- `backend/app/api/maintenance_acceptance_checklist.py`
- `backend/app/api/maintenance_bad_returns.py`
- `backend/app/api/maintenance_bad_salvage.py`
- `backend/app/api/maintenance_ckd_import.py`
- `backend/app/api/maintenance_demands.py`
- `backend/app/api/maintenance_return_receipt_import.py`
- `backend/app/api/maintenance_return_receipts.py`
- `backend/app/api/maintenance_warehouse.py`
- `backend/app/api/replenishment.py`

三组API文件两两不交；共39文件、239入口，228 human +11 retired。原逐入口说明仍在s3-resource-seed-gaps.json；新增拆分资源只变授权绑定，不制造新业务端点。

## 批量对象与读取一致性补充

`foundation_interfaces` 是实施签名的唯一设计源：单个真实对象由同一快照的布尔关系属性送按最多50对分块的批量 CheckResources（最少 ceil(pairs/50) 次，同一观察、任一失败整批失败）；集合使用 PlanResources 及上述派生子查询。`ContextAccessSet` 按 `(context, resource, action)` 索引，不按 resource 覆盖多个对象。对象集合不允许借 module_probe 得到业务 allow。

纯读的多对象、文件依赖、列表 capabilities 使用 root 提供的短独立 REPEATABLE READ READ ONLY 快照及绑定 Session/活动事务的 ReadSnapshotProof；actor、Grants、完整 parent/关系和该次数据投影从同一观察读取，不显式持 actor/domain 锁。事实 SQL 与 DTO 物化均在该事务内完成，不能把 predicate、懒加载 ORM 或 proof 交给外层 READ COMMITTED 使用。写命令一次收齐完整 topology 的领域锁后才取 actor SHARE；callback 只用当前事务及已有锁，不能换连接或逐对象追加锁。特别 receipt 的 project SHARE 不能稳定其 parent：移动写也可持 project SHARE 再改 child，因此必须稳定 child 与旧/新 parent 或使用一致快照。

HTTP context 只有 `kind=project|collection|object`；所有子对象具体类型走 closed `object_type`。内部 typed Ref 可扁平 kind，但必须由服务端明确转换，TS enum 从 full spec 生成。所有 scope，包括 all，都需要真实 typed domain_context；裸 object_id/revision 不算 proof。无 query_attributes 仅限注册 context-only 资源。

主体持久化统一依据 [sidecar schema](s3-artifact-sidecar-schema.md) 的六个 owner 列及身份门清单。领域服务使用稳定 user_id，不以 username 构造 canonical 命令去重键；历史用户名仅供展示和审计。D03 文档依赖保留真实 carrier 和完整 parent 拓扑 revision，不能仅存获权 project 后丢失文档/链接变化。
