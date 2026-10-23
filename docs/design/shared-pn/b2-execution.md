# B2 宿主协调、任务队列与 OSS 执行

本阶段仅为本地候选；没有生产数据库迁移、OSS 请求或服务切换。旧治理页面的请求尚未接入新的预览基线，后续必须由界面携带真实读取版本，不能在服务端临时补一个新版本冒充用户确认。

## 模块边界

- `pn_catalog.jobs` 负责持久任务、短事务领取、执行租约、取消、重试和完成回执；不识别 OSS 或业务事实表。
- `services.pn_catalog_identity` 负责 PN 合并、撤销合并、别名改指。调用既有无内部提交的业务服务，在同一事务保存身份／资料版本、审计、请求回执和重算任务。当前权限必须同时允许 `manage_identity` 与 `govern_facts`，并覆盖所有受影响 PN。
- `services.pn_media_cloud` 是 shop 现有 OSS V4 和 FC HMAC 协议的窄适配器，仅提供本阶段需要的能力；没有跨仓库 Python 导入，没有本地图片处理后备路径。
- `services.pn_media_execution` 连接队列和媒体发布器。`prepare` 在数据库会话关闭后执行 OSS／FC 操作；`verify_resource` 与 `publish` 在新的事务中完成资源和租约校验、原子发布。媒体执行器在每次 FC 调用前重新鉴权并续租；OSS 使用任务局部 guard，分页和逐个 abort 的每次 SDK 网络请求也检查租约，纯本地签名无需网络 guard。

## 并发与恢复

身份命令需要每个受影响 PN 的 `identity_revision` 和 `profile_revision`。预览返回完整 PN 集合；应用时集合或版本变化都拒绝。锁序为账号权限／请求回执、`DATA_CHANGE`、身份拓扑、排序后的 PN、业务事实。撤销合并遇到后来已经重新归属的事实行会返回冲突，不覆盖后续治理。

队列在任何 provider 调用前先执行短事务 preflight，复核当前权限、资源版本和租约；失效时不出网。发布时再复核。

任务输入、来源和逻辑 ID 在数据库中不可修改；重试只创建新的执行轮次。队列使用 `FOR UPDATE SKIP LOCKED` 领取任务，领取后立即提交。发布必须同时匹配 owner、随机 token、递增 epoch、attempt、尚未到期的租约以及当前资源 generation 和 input revision；已取消、过期、被重领或资源改变的结果不能发布。网络超时可能留下未引用的不可变 OSS 对象，但不会把旧对象挂回图库。

任务 `revision` 随状态事件递增。公开取消／重试同时要求 `expected_revision`、`expected_attempt`、`expected_retry_count`，并复用全模块请求幂等键。丢失响应后重放只投影当前状态，不会再次取消后来启动的重试。内部媒体取消是针对整个逻辑任务的服务调用。

重算任务在宿主执行利润、库存成本、维保成本三个引擎的 `commit=False` 变体，结果和任务完成状态一起提交。执行失败会整体回滚、保留待重试任务。长期批量重算仍占用已有数据变更锁；当前测试证明的是事务正确性，不代表生产数据量下的时延。

分片上传保留原 OSS upload ID。初始化丢失响应可查询并恢复；合并丢失响应通过固定 staging key 的 HEAD、声明字节数和票据指纹判断已经完成，再推进验图。取消在同事务入队 abort 任务；初始化返回晚于取消时，仅清理返回的孤立 ID，不能扫描并删除获胜会话。

## 配置与启用

`MediaCloudSettings` 只读取 `PN_MEDIA_` 前缀，`PN_MEDIA_ENABLED` 默认 false。

| 变量 | 用途 |
| --- | --- |
| `PN_MEDIA_OSS_ENDPOINT` | HTTPS OSS 服务源站 |
| `PN_MEDIA_OSS_BUCKET` / `PN_MEDIA_OSS_REGION` | Bucket 与地域 |
| `PN_MEDIA_OSS_ACCESS_KEY_ID` / `PN_MEDIA_OSS_ACCESS_KEY_SECRET` | 静态凭据；签名由本地 SDK 计算 |
| `PN_MEDIA_CLOUD_IMAGES_URL` | 现有 FC HTTPS 源站；普通图片调用 `/v1/process`，打包调用 `/v1/bundle` |
| `PN_MEDIA_CLOUD_IMAGES_SECRET` | FC HMAC 密钥，至少 32 字符 |
| `PN_MEDIA_CLOUD_IMAGES_TIMEOUT_SECONDS` | 整体调用时限，默认 60 秒，上限 90 秒 |

配置不足时明确返回 `media.provider_not_configured`。FC 请求不跟随重定向，只发送有界 JSON，响应上限 64 KiB，并使用整体超时；上游正文、签名地址和凭据不进入公开错误。临时链接按请求新签发，不能写入持久回执。

路径兼容现有 FC：暂存 `uploads/pn-catalog/...`、原图 `img/pn-catalog/...`、缩略图 `thumb/pn/.../thumbnail-600-webp85-v1/thumbnail.webp`。原图、缩略图配方和存量对象 key 分离，迁移不重命名旧 key。

## 已验证与边界

使用隔离 PostgreSQL、MockTransport、OSS SDK 的本地签名和假 provider 验证，没有真实云端访问。覆盖并发领取、过期重领、取消／重试、状态 ABA、授权撤销、事务回滚、迁移保护、身份预览冲突、真实派生成本重算、分片恢复，以及 FC 协议和输入／返回限制。

现阶段 handler 注册包括 `media.verify_upload`、`media.thumbnail`、`media.complete_multipart`、`media.abort_multipart`、宿主 `identity.recalculate`，以及三种人工辅助任务 `enrichment.recognize`、`enrichment.research`、`enrichment.bundle`。独立 worker 的宿主安装和 B3 当前权限解析由 runtime 适配；API 进程不自动启动任务循环。跨仓库共享发布包、worker 常驻进程部署、生产配置、旧 UI 切换及真实 OSS／FC／模型验收仍待后续阶段，不能把本地 fake provider 测试当作生产证明。

## 人工辅助流程

`pn_catalog.enrichment_dto` 是有界命令契约，`services.pn_catalog_enrichment` 管理基线、权限、并发配额、审计和入队，`services.pn_enrichment_execution` 在每次出网前重新核对来源账号权限、资源和租约。原始账号、授权 epoch/mapping 声明完整保留，执行器不能将自己的服务身份替换为原发起人。

- 识别：需要 inbox revision，只处理 ready、尚未归档的图片；最多返回 10 条 PN 提示及分类提示，不自动绑定 PN。视觉模型总调用期限最多 45 秒，同一进程模型槽最多 2。
- 研究：需要 identity、profile、所选能力的图库／好坏件内容版本；同一人或同一 PN 的同一种能力最多一个未完成任务。最多 2 次搜索、24,000 token 预算、100 次外部调用，整体 600 秒。每次出网先持久预留，真实 usage 返回后按租约结算；响应丢失时保留预留，自动／人工重试均不清零。普通模型没有通用精确 tokenizer，输入预留为保守估计；供应商超额计量会完整记账并阻止后续调用，不能声称是供应商计费硬上限。
- 文字结果只调用候选接口：来源在搜索证据内、当前缺失且未受人工保护的字段进入待采纳候选；规格仅接受当前受支持键且不恢复已删键。研究记录保留来源网页、字段置信度和证据索引；候选记录保留原账号、执行器、provider/model/run/template，需另行人工采纳。
- 图片结果只发布 candidate GalleryEntry，要求来源来自可验证网页图片列表、没有报告外来水印，通过 FC 导入、完整验图及不可变复制；不自动变成正式图库图片。已有同 hash 删除记录阻止恢复。
- 打包：可按好／坏件用途及可见 entry IDs 选择，最多 1,000 张、总量 1 GiB；每卷最多 100 张／128 MiB。同一人最多 2 个、全局最多 100 个未完成打包任务。结果最多保留一天，新签下载地址最长 900 秒；签名前再次核对来源 entry、generation、图库／身份版本和当前权限。删除后拒绝新签名，先前已签发链接在短 TTL 内仍可能有效。

普通 GET 只读取持久状态，不触发模型、搜索或新打包。三个 POST 先检查宿主 capabilities，缺少配置返回 `enrichment.not_configured`，不入队。

研究模型适配器从 shop 已有协议提取，支持 GLM Web Search、DeepSeek Anthropic server tool、Qwen 原生 DashScope 的结构化来源；模型正文中的 URL 不能伪装为搜索证据。所有服务默认关闭，使用显式 `PN_ENRICHMENT_` 配置：`ENABLED`，`SEARCH_MODEL_PROVIDER/NAME/API_KEY/BASE_URL`，`TEXT_MODEL_*`，`VISION_MODEL_*`，`RESEARCH_TIMEOUT_SECONDS`（默认60、最多90），`VISION_MODEL_TIMEOUT_SECONDS`（默认45、最多45）。完整环境变量如 `PN_ENRICHMENT_SEARCH_MODEL_NAME`。媒体服务另需 `PN_MEDIA_*`。配置由服务端提供，不允许客户端请求携带 provider 凭据或任意服务端点。

研究与识别 HTTP 使用无重定向、禁环境代理、有界非压缩响应和整体超时；外部图片仅允许公开 HTTPS 来源，FC 仍负责 DNS 和逐跳地址检查。全部本地测试使用 MockTransport 和假 OSS／FC，真实账号配额、端点兼容性、网络时延及常驻多 worker 运维尚未验收。
