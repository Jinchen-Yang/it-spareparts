# B2 媒体模块实现边界

候选实现位于 `backend/app/pn_catalog/media.py`，所有公开写命令接受 `context`、`authorize`，不 commit、不执行云端 I/O。HTTP/BFF、权限框架与 worker 的装配由宿主负责；生产配置和旧 shop 路由尚未切换。

## 已实现的事务内能力

| 工作流 | Facade | 关键约束 |
|---|---|---|
| 图库读取、候选/删除区、用途筛选 | `get_gallery` | 50 默认 / 200 上限；数据库先授权过滤、隐藏/去重/排序后取页；768 KiB 动态字节预算；Asset/Derivative 批量读取 |
| 图库归档与复用 | `add_entry` | 固定上传目标、身份版本检查；复用需来源可读且可复用；未定目标上传只能走收件箱归档 |
| 修改、删除与恢复 | `patch_entry` / `delete_entry` / `restore_entry` | 条目 ID 保留；tombstone 阻止旧票据和改用途绕过；恢复必须删除版本和原因 |
| 排序、主图、组级隐藏 | `arrange_gallery` / `suppress_hash` / `unsuppress_hash` | 完整成员集检查；图组隐藏保留来源条目；重复导入不撤销隐藏 |
| 上传 | `create_upload` / `complete_upload` / `cancel_upload` | owner+client+client_request 唯一；验证工作持久入队；取消代次拒绝旧结果 |
| 断点续传 | `start_multipart` / `record_multipart_part` / `complete_multipart` | 同一 OSS upload ID；实际 OSS 分片与客户端声明比较；合并及取消清理持久入队 |
| 收件箱 | `list_inbox` / `get_inbox` / `claim_inbox` / `release_inbox` / `assign_inbox` | 收到已验证资产后才共享；SQL 范围过滤先于分页；认领过期 CAS；归档与图库、审计一次提交 |
| 连拍分组 | `create_inbox_group` / `add_group_item` / `arrange_inbox_group` | 分组版本与完整成员顺序；保持同件连拍/下一件语义 |
| 收件箱删除、恢复 | `delete_inbox` / `restore_inbox` | 原图不删除；新代次/新认领；恢复不恢复旧归属 |
| 缩略图 | `create_derivative` | 不可变 recipe；任务检查条目代次及 PN 身份；ready 不修改图库编排版本 |
| 动态图片链接 | `entry_links` | 当前权限和删除/隐藏状态检查；本地签名 300 秒；回执不保存 URL |
| 身份合并／撤销 | `ensure_identity_gallery_members` / `merge_gallery_groups` / `unmerge_gallery_groups` | 从变更前已锁定拓扑初始化历史成员；不自动修正冲突；来源条目不搬，隐藏意图传播到撤销两侧 |
| 人工研究图片落库 | `publish_research_candidate` | 可信 worker 验图结果与固定 job/index key；初始图库版本 0；只能产生 candidate，删除意图不可被新研究恢复 |
| 打包快照与重验 | `bundle_snapshot` / `verify_bundle_snapshot` | 全组选图权限、条目代次与身份/图组版本；好件排除 defect；总上限 1000 图/1 GiB，宿主按 100 图/128 MiB 分包 |

写回执与 B1 共用 `(principal_id, client_id, request_id)` 全局空间。操作当前授权先于重放，合法重放先于旧版本校验；原 applied_revision 与当前 data_revision 分开。跨来源复用回执保留源 Entry/Asset 引用，并在重放和 GET 回执时重新核对来源权限。

## 宿主和队列接缝

权限 adapter 必须返回类型化 `AuthorizationDecision`。字段能力使用 `gallery`、`uploads`、`inbox` 或明确的 `*`；具体 action 区分 `gallery.archive/read/reuse/edit/adopt/change_purpose/delete/restore/arrange/suppress/unsuppress/derive`、`uploads.create/read/complete/cancel/manage/multipart.*`、`inbox.read/shared.read/claim/release/assign/delete/restore/takeover/manage/group.*`。共享收件箱 SQL 范围使用已验证的 `metadata.scope=all` 或 `metadata.owner_principal_ids`；未接入范围时拒绝共享列表。

外部调用使用 `media_ports.Storage` / `CloudImages`，接口沿用 shop OSS/FC 语义。宿主取得不可变 plan 后先结束 DB 事务，再调用云端；结果由下列 publisher 在 queue 的资源及租约双重 fencing 后落库：

| Job kind | Verify resource | Publish |
|---|---|---|
| `media.verify_upload` | `verify_upload_resource` | `publish_verified_upload` |
| `media.complete_multipart` | `verify_upload_resource` | `publish_multipart_completed` |
| `media.abort_multipart` | `verify_multipart_abort_resource` | `publish_multipart_aborted` |
| `media.thumbnail` | `verify_derivative_resource` | `publish_derivative` |

锁顺序为身份拓扑 → 按 ID 排序的 PN → 媒体父资源 → Job。云端验图/不可覆盖复制、缩略图和 multipart 合并/终止均不在数据库事务中进行。初始化分片的迟到结果若不能发布，宿主必须终止刚返回的孤立 upload ID；已取消的已知 upload ID 有持久 abort job。生产许可/服务暂停由宿主 adapter 映射到队列的 provider-wait 状态。

为了复用现有 FC allowlist，新上传临时键为 `uploads/pn-catalog/{upload_id}`，原图为 `img/pn-catalog/assets/{upload_id}/original`，派生图为 `thumb/pn/{asset_id}/{sha256}/{recipe_id}/thumbnail.webp`。recipe 初版为 `thumbnail-600-webp85-v1`，复用现有 600px WebP/85 处理；原设计的 `derived/...` 示例在这里放入 FC 已允许的 `thumb/` 前缀内。旧 OSS key 原位保留。

## 迁移与验收边界

`a3c6e9f2b5d8` 依赖内容迁移。Asset 不可变 trigger 阻止更新和删除；有已采用数据时拒绝有损降级。历史 Asset 的 `verification_status=legacy_metadata`，ETag 可空，绝不把历史元数据写成已经通过新 FC 验图。新上传为 `cloud_verified`，必须有 ETag。

旧 Image UUID 原样作为 Entry ID。`origin_namespace + origin_id` 保留原 shop PN UUID，`origin_part_id` 可空，空值只读；多个旧来源归到同一 PN 时保留各自 Entry，展示另做去重。来源+hash+purpose 唯一约束覆盖 tombstone；正常新增在 PN 锁内检查同 PN/hash 并保留删除意图。迁移导入器由宿主负责，不读真实 OSS、不改现有原图。

历史缩略图允许多个 Asset 引用同一 object key，沿用旧库合法状态；新生成配方依然以 `(asset_id, recipe_id)` 唯一并使用包含 Asset/hash 的目标 key。物理对象共享不抹掉任一来源条目。

媒体领域、HTTP、独立复现与缩略图迁移兼容测试共 39 项通过，覆盖真实 PostgreSQL 并发、回执/审计回滚、权限撤销、删除恢复及迟到 worker、图库分页/查询次数、共享收件箱范围/认领、multipart 实际分片校验。`http_media.py` 已暴露类型化接口，shop 已生成相应 BFF；辅助工作流宿主另行接入。真实权限桥接、两站页面切换、OSS 验收和统一用户验收仍待后续阶段；此候选不代表完整替换或生产上线。
