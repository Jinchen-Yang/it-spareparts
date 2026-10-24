# S3 仓库 6 API 实施与验证

范围：`maintenance_warehouse.py` API/业务服务与新增 `maintenance_warehouse_authorization.py`；前端 S3 已冻结。本记录不是远端 CI、浏览器验收或生产部署证据。

| 实际入口（均 POST /api/maintenance） | canonical 合同 | 实施边界 |
| --- | --- | --- |
| `/warehouse-imports/preview` | `maintenance_warehouse_import.preview` | 真实上传 bytes 解析；operation_input；授权视图和正式预览均不写 job、批次、审计、sidecar |
| `/warehouse-imports/{import_id}/apply` | `maintenance_warehouse_import.apply` | 实际行存在性选择 new/existing；bytes+adapter+HMAC；全目标关系；稳定 owner；不可变回执与 sidecar；当前授权和内容拓扑重验 |
| `/warehouse-documents/search` | `maintenance_warehouse.read_documents` | 当前文档与实际 source/project 关系 SQL 范围先于 total/分页；同只读 RR 内事实物化 |
| `/warehouse-ambiguities/search` | `maintenance_warehouse.read_ambiguities` | 独立动作；相同真实文档范围；未归属歧义仅 PDP 完全允许的范围可见 |
| `/warehouse-ambiguities/{ambiguity_id}/resolve` | `maintenance_warehouse.resolve_ambiguity` | 真实 ambiguity、原文档和所选实际候选共同解析；父项目/源单/部件/目标/关联代次锁；锁后当前账号授权 |
| `/warehouse-integrations/reconcile` | `maintenance_warehouse_import.reconcile` | discovery 不授权写；完整实际 document 集合+operation_input plan digest；空集仍需独立动作；默认领域函数不变 |

共享协议来自 `context_registry`、`maintenance_authorization` 和 `authorization_artifacts`；没有新 HTTP 权限探测端点或第二份资源目录。所有同路径 `view=authorization` 返回最小 locator/context；新客户端回同一 carrier precondition。未知 view、输入变化、范围变更失败关闭，409 不自动重放。

## 查询与锁

canonical 查询通过专用 QueryBindings 把实际 warehouse document 的全部直接 project、当前 source assignment、site issue/bad return、递归 upstream warehouse document 项目关系映射为 PDP 属性。primary_managed_projects 不能借旧 project link 读取已移父单据；谓词先于 count/page。同一只读 Repeatable Read 内完成授权、范围与 DTO 物化。

写入口先做只读授权预检，再取现有 DATA_CHANGE advisory lock、上传业务身份锁、仓库投影 integration lock、完整实际领域行锁，最后锁定 actor 并重算对象集合/关系/Grant。回执、关联变更及副作用同一事务提交；失败整体回滚。`reconcile_project_assignment_links` 的默认纯领域调用语义保持，source/master 调用没有被隐式加上新的 HTTP 权限门。

## 回执

`MaintenanceWarehouseImportBatch.owner_user_id` 由根代理增加 nullable FK。canonical 新导入首次 INSERT 写 stable owner；旧 NULL 不回填、不按 applied_by 猜身份。原 `(source_file_hash, adapter_version)` 唯一性保持；其他账号不能因同用户名/同文件而认领。

批次与审计有真实数据库不可变触发器，因此授权合同通过可选内部回调在首次审计 INSERT 前写入，禁止事后 UPDATE 历史。sidecar 的 provenance 重新解析持久 `warehouse_import`，不保存临时 operation_input 作为授权证据。不可变回执内容与当前完整父关系共同进入不含 actor 的 content_revision；原账号改名可继续回放，移父/关系变更使旧内容证据失效。

## 验证

- 真实 PostgreSQL + pinned Cerbos 0.57.0 新专项 16 cases；每个 API 有真实 HTTP/PDP 路径。
- 4 个既有 warehouse API/import/adapter/bridge 文件 42 cases，原断言保留。
- 合跑 57 passed：`/tmp/itsp-s3-warehouse-combined-final.log`（33.50s）。
- 最终增加实际候选行/alias 锁、递归 upstream 父闭包后的专项复核：`/tmp/itsp-s3-warehouse-upstream-green.log`：16 passed（30.85s）。
- 关键反例：preview/view 零写；无独立动作拒绝；错误 bytes/HMAC、stale revision；readonly 自定义 Grant；总数分页前范围；上游单据完整父范围与循环终止；真实项目锁等待时撤权零写/移父 409；用户名重用/旧 NULL owner 不认领；sidecar hash 伪造；稳定 owner 阻止丢失 schema downgrade。
- 新非空 reconcile 反例曾证明仅绑定不可变 result 不足以约束旧回执；修复 content_revision 后原反例通过。

待独立交叉审查和根代理最终集成 CI；未提交、未推送、未部署。

独立补证：`test_authz_s3_independent::test_warehouse_real_manifest_publication_failure_rolls_back_business_and_lineage` 在真实 `publish_artifact` 已生成 complete manifest 后抛异常，Batch、Document、Link、Audit、sidecar 全部回到原计数；1 passed，日志 `/tmp/itsp-s3-warehouse-independent.log`。当前 `content_revision` 包括 project content revision，纯备注等项目版本变更也可能使旧回执保守失效；此结果是来源一致性失效，不等同于身份或权限撤销。
