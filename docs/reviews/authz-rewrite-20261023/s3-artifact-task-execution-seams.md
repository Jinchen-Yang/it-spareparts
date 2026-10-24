# S3 文件与任务接缝（只读准备，未实施）

本文件与 `/tmp/s3-backend-entry-dependency-groups.{md,json}` 对拍，补充其工作簿/来源包及流程包的文件边界。当前 239 入口清单仍是拟定 S3 合同；以根代理明确 S2 精确提交 CI 全绿为实施前置。没有修改应用、模型、迁移或测试。

## 一条发布链，多个不可合并的业务门

入口实名身份 → 当前业务资源/项目谓词 → 生成或解析 → 保存来源清单 → 发布前重验 → 每次返回字节/回执时重验。生成成功、owner 相同、HMAC 正确、预览通过都不能替代后续读/写授权。工作簿不是任意项目 JSON 的另一种序列化：它包含签名元信息、隐藏页、原始列和固定格式字段；不能对其中一页的授权推导整份内容权限。

| 现有出口 | 必须保留的不同旧门；不能合并 seed |
|---|---|
| project master V1/V2 | D03 真实项目经理完整内容例外与普通销售/通用分支不同；普通下载 profit/direct，写入另有 upload+profit 或 D03 editor，合同额变更还有独立资格 |
| expense-collection workbook | finance download profit，apply upload+profit；不继承 D03 manager 例外 |
| 旧 project/workbook roundtrip | `_require_roundtrip_permissions` 的非 scoped、cost+profit+customer 门；validate/apply/errors 仍有各自操作要求 |
| project workbook-v3.xlsx | 实时账号 cost+profit+direct；与旧 roundtrip 的 customer/scoped 门不同，应采用具名动作，不用同一 download seed 掩盖差异 |
| manager monthly v3 | adapter `_owner()` 还检查 `contract_amount` 可见，所有 status/export/validate 都经过该门；本人 active primary 管理项目集合；apply 再需 manager_workbook_apply+profit。不可误套 D03 的“经理无 profit 也完整” |
| global lines/master/batch transfer | 全局目录/行范围和 profit、写动作按原入口分别判定；下载表单/字段选择不是后续 apply 权限 |
| receipt/warehouse/CKD/WBDD originals 与历史回执 | 各自动作、来源所有者和实际关联项目；不得由普通项目 read、另一导入动作或预览可见前 N 行推导 |
| acceptance / collection evidence | 当前 active link、实体和项目关系、文件 active 状态、完整内容资格；上传人是审计事实，不自动变成永久下载权 |

来源：`api/maintenance_*workbook*.py`、`services/maintenance_manager_workbook_adapter.py:_owner`、`api/maintenance_return_receipt_import.py:original`、`services/maintenance_acceptance.py:controlled_download`。上述旧门已通知后端合同作者同步其 seed gaps。

## 共享接口与保存结构：一个 owner 先落基础

建议扩展已有 `authz/provenance.py`，不另造文件权限解释器。现在 schema 1 仅有 resource/actions/grant_version，`AccessCollector.record` 拒绝 object_id，不能把 S3 项目对象当作 all 来复用。

1. 保持 `service.access_recorder(callback)` 为实际允许读的唯一来源；扩展不可变 ResourceAccess/collector 的对象依赖，保存 `(resource_id, object_type, object_id, authorization_revision, actions, grant_version)`，同资源不同对象分别保存。现有 schema 1 全局依赖继续可验证；对象依赖需要显式新版本，未知/缺失形状拒绝。
2. 保持 `validate_provenance(db, identity, value)` 对外入口；由服务端 registry 调用根所有的项目/来源绑定适配器，从 DB 重新加载关系和完整 bindings，再执行 `resource_access`。客户端、工作簿单元格、存量 JSON 不能提供可信 SQL、关系 attributes 或任意 resolver。
3. 生产者身份与读取者授权分开。Agent 派生产物现为本人且 epoch 精确匹配，继续保留；项目共享附件按文件当前实体 ACL 授权读取者，不能要求读取者等于原上传人。上传人/作业 owner 用稳定 SysUser.id，显示名/username 只审计。当前接口仅接受 username 型 VerifiedIdentity，持久任务恢复需要根提供服务器专用 `resume_task_identity(db, owner_user_id, enqueued_epoch)`：严格检查 enqueue epoch，不能用当前 epoch 给旧任务续命，不能从请求体构建。
4. 文件清单至少包含 format/protocol、bytes hash、stable owner（有 owner 语义时）、来源对象依赖全集及内容完整性标志。验证计划/错误工作簿/回执各保存自己的清单，不只保存一个当前网页数据版本。已有 `MaintenanceManagerUploadBatch.owner_user_id` 可复用；`MaintenanceProjectWorkbookValidation.created_by`、CKD/receipt batch.uploaded_by、BusinessFile.uploaded_by 等字符串不能被当作稳定 owner。
5. 迁移由根单一 owner 实施：按上面语义补 owner FK/provenance JSONB（SQL NULL 表示未知），不覆盖现有审计字符串。历史 username 不具备唯一身份历史证据时不猜映射；未知来源新权威保守拒绝使用，明确重新生成/重新验证。不要批量把旧 NULL 变成可信空依赖。

项目关系 revision 使用已批准 opaque 指纹并每次完整重授权，不能拿 workbook data_version、project.version、actor epoch 当关系版本。指纹不宣称能识别未证明的 A→B→A 历史变更；如产品要求历史关系曾变即永久失效，必须另落单调关系修订机制。

## 队列与文件边界落点

| 实际接缝 | 改造步骤及必须保留的业务行为 |
|---|---|
| project `_validate_project_workbook_in_worker`、manager `_validate_in_worker` | typed identity 传入真实线程，线程自己建 Session、安装 recorder；读取文件/领域数据前重验，写 validation/error artifact 前重验；继续等待取消后的 worker 终态再清临时文件 |
| receipt `enqueue` → `run_job(batch_id,generation)` → retry/apply | 现 generation CAS 只防旧 worker，不是权限版本。enqueue 保存 owner+epoch+任务动作；开工、解析后读领域数据前、ready/preview_token 发布前分别重验；retry 捕获当前已验证身份，不能复用旧 epoch，也不自动升权 |
| 旧 `_import_roundtrip_in_worker`、warehouse `_apply_worker` | 去掉把 ctx/操作人字符串当允许凭据的入口；保留实际逐行/整份事务边界。领域锁后，稳定顺序锁全部参与用户最终强度，再用 locked_user 复核；不在 HTTP dependency 预锁 actor |
| exporter/renderer → Response/StreamingResponse | 查数据前按每个项目选完整授权合同，不能把 A 的 manager scope 与 B 的普通字段 union；先生成未发布 artifact，短事务重验后发布；每 chunk/实际发送边界重新校验，不能持业务锁跨慢下载 |
| error workbook、历史 result_json、幂等 replay | 先实时 owner/项目/动作检查，再返回完整历史内容；已应用只表示无需再次写入，不表示可无条件看回执。状态/计数/差异与文件内容同样可能含敏感事实 |
| acceptance `file_project_id` → `controlled_download` | 统一解析同一有效 link；重验 link/entity/project 变化，不能先按旧 project 授权后对另一条 link 返回内容。保留 hash/size 校验与下载审计，字节发送前再次重验 |
| Agent 三个维护工具、AI fallback 采样 | 工具仅绑定受保护的维护 facade；记录实际对象依赖，不以工具名/零 callback 判安全。AI proposal 外发字段样本前和模型返回/保存前各重验；不持锁跨模型等待 |

持久 artifact protocol 使用哨兵应沿用 S2 模式：独立短事务先于任何 domain/actor 锁，避免 receipt→user 与 user→receipt 的跨连接环。S3 字段/文件格式一旦发布，降级不能擦掉来源后把旧文件重新解释为安全 legacy；纳入根单一迁移的 guard/receipt。

**已确认的原件边界**：return receipt `/original` 的 `public_job(limit=1)` 会检查全部已形成 plan rows，并非只校验首行；但 queued/failed 且没有 rows 时仍不能证明原件完整范围。未知/未解析行、额外页和原始敏感列不能变成空依赖。既有 FileResponse 还需要逐块保护。可保留原文件审计存储，但不能据此发布下载；完整原件资格应由 `download_original` 对整个内容域负责，部分预览授权不足时拒绝原件，不做静默裁切。

## 可执行任务拆分与最小验收

- **A 根/shared**：冻结对象 binding/revision、typed 持久任务恢复、provenance 对象扩展与单迁移。先测同 resource 两对象、未知来源、owner 重名/重建、撤配置不回 legacy、PDP/DB 故障不是 deny-empty。
- **B workbook/source owner**：上述六类工作簿各绑定精确动作、字段与范围；分别接 validation/apply/error/replay。经理在 A、销售在 B 且 profit=false 的向量必须禁止跨项目拼权限；旧与 v3 下载差分必须明确。
- **C flow/file/task owner**：receipt/warehouse/acceptance 等原件、队列、下载接入同一来源与重验接口，保留 generation、幂等和领域提交语义。根 migration 只接收此包明确模型字段需求。
- **D 非 HTTP 与独立复核**：真实线程/第二连接，在 enqueue 后撤权、render 后关系改变、第一 chunk 后撤权、apply 等领域锁时撤权；全部旧回执 no-op 路径同样拒绝。测试 multi-project 一项失权即整原件拒绝、同名换账号不得继承 owner、link 改挂后不能下载旧授权内容、失败不会发布 token/路径或成功回执。

这些是接缝和验收任务，非 S3 实施证据。通用 imports.py 的长期 daemon/作业体系也有同类字符串 owner 边界，但不得借此扩张当前 239 入口 S3 范围；它属于其实际资源阶段再接入。
