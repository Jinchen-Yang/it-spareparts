# 主数据库目录与人工提交契约 v2

- 版本：**v2.0-draft**；日期：2026-10-15；状态：**拟定，尚未实现**。本文 URL、字段、容量默认值、签名协议和错误码是供后续实现共同遵循的工程拟定，不表示现有路由、测试、用户验收或发布完成。
- 决策：D-36（`docs/decisions/0014-主数据库与商城目录互通.md`）；目录契约 owner 为 P5，消费者为 P6。两仓 `docs/integrations/catalog-v2.md` 必须逐字节相同；owner 先改规范，再同步副本与生产者／消费者测试。
- 来源：2026-10-15 开工计划 §三、§五，P5 边界 §3、§5，P6 边界 §3、§5及《合并-目录同步-调研》§3.2–3.6；业务以《金臣答复-2026-10-15》§13–16为准。本文冻结调用者所需的版本、顺序、错误和恢复规则，不规定内部表名。

## 1. 权威、方向与兼容边界

备件系统的 PN 主数据称“主数据库”。主数据库拥有稳定身份、规范名、别名和通用资料；商城拥有商品扩展、库存、图库、文案与 AI 候选。保留两个数据库，只通过服务接口通信。

主数据库自动发布全部节点、全部状态和类型。商城以 `(source_namespace="itsp", part_id)` 识别来源：active（含待审和排除）可建好坏双零库存 SKU，merged 保留身份墓碑并归入 canonical 商品；既有库存不得清零。改名、合并、撤销合并和生效别名自动镜像身份，保留原商城 UUID、SKU、流水、原件、图片和文案历史，不产生数量流水或物理搬账指令。pending、rejected、null 别名只是来源记录，只有 active 可用于确定性解析；下单 PN 和相似文本不构成等价关系。

商城仅由数据处理页实名人员选字段并确认，向主数据库发起一次受控提交；“只保存在商城”独立于此动作。AI、搜索、找图和文案获取只能由人另行点击，结果默认留商城；目录同步、建品、读取、命令成功都不授权研究。目录及普通资料命令不传采购、销售、库存、项目、审批／申请状态、价格、人员、客户、合同、SN或原件。业务智能体写业务状态的禁令继续有效。

图库由商城拥有，主数据库按稳定 part_id 只读关联，详见同目录 [gallery-v1.md](gallery-v1.md)。历史参考价仍沿原点击查询链路，不随目录批量拉取。

## 2. 通用 JSON 形状与版本

新接口前缀为 `/api/integrations/catalog/v2`，HTTP JSON 为 UTF-8、`snake_case`，`Content-Type: application/json`。成功信封必含 `schema_version:2, source_namespace:"itsp", request_id, correlation_id, generated_at`；涉及发布的数据另含 `publication_epoch`。日期用 UTC `YYYY-MM-DDTHH:mm:ss[.ffffff]Z`。`request_id/correlation_id` 为 UUID 字符串；`publication_epoch` 是服务端生成的 UUID，重建发布世代时更换，不能拿大小比较。

下表构成本文的字段 schema。未标可空的字段必传且不允许 null；`?` 表示可省略。JSON 整数范围 `0..9007199254740991`，ID／revision 必须为正（初始水位允许 0）；不得经浮点转换。`numeric_value` 用十进制字符串。请求中的未知字段拒绝；响应仅允许成功信封顶层的可选 `extensions:object` 承载可忽略扩展，不得为节点／别名／规格／逻辑批新增未声明字段。未知 schema、必需语义或枚举值须停批报警，不猜默认值。

| 对象 | 字段／类型与不变量 |
|---|---|
| node | `part_id:int, pn_std:string, status:"active"\|"merged", merged_into_id:int\|null, canonical_part_id:int, needs_review:bool, is_excluded:bool, machine_or_part:string\|null, description:string\|null, brand:string\|null, category_major:string\|null, category_minor:string\|null, unit:string\|null, locked_fields:string[], specs:spec[], aggregate_revision:int, identity_revision:int, content_hash:sha256`。active 的 merged_into_id=null、canonical_part_id=自己；merged 的目标必须存在且最终根为 active，不允许环。merged 是保留占名的身份墓碑，不是删除节点。类型原值及 null 完整传输，不自创品类。 |
| spec | `key:string, value:string, unit:string\|null, numeric_value:decimal_string\|null, source:"auto"\|"manual"`；每节点 key 唯一。目录完整发布已有键，包括人工命令白名单之外的历史键，不截断；`specs:[]` 表示参数已空，完整节点缺某键即该键已删除。 |
| alias | `alias_id:int, pn_raw:string, part_id:int, pn_std:string, status:"active"\|"pending"\|"rejected"\|null, deleted:bool`。null 保留历史语义；deleted=true 仍带删除前归属，不能把它当成有效别名。未删除别名的 part_id／pn_std 必须对应所属节点。 |
| item | `{"kind":"node","node":node}` 或 `{"kind":"alias","alias":alias}`。全量只含当前节点及未删除别名；增量含完整受影响节点（完整 specs）、相关别名及删除标记。别名换属需同批给出新归属和 `alias_moves:[{alias_id,old_part_id,new_part_id}]`，两父节点均在批内。 |
| version | aggregate_revision 覆盖资料、参数和别名真变化；identity_revision 覆盖规范名、重定向、合并／撤销和生效别名关系。单调递增但不保证恰好 +1 或连续，不能靠差值判漏。响应不得用迟到版本覆盖新版本。 |
| sha256 | 64 位小写十六进制，算法为 §6；不含 `sha256:` 前缀。 |

node 的品牌、品类取主数据库 PN 文本列，不隐式 join 字典名称。字典改名要影响公开目录须通过显式 PN 更新；人工编辑必须同时重绑／清空字典 ID、更新分类来源并留审计，不能只改文本留下旧 ID。

## 3. 目录读取与恢复

全部读取使用独立 `catalog:read` 服务凭据。成功读不改业务资料、不联网研究。发布底座未就绪返回 503 `publication_not_ready`；功能未开返回 404 `integration_disabled`。

| 方法与路径 | 请求及成功响应的增量字段 |
|---|---|
| `POST /snapshots` | 请求 `{"schema_version":2,"request_id":UUID}`；按 `(client_id,request_id)` 复用同一快照，同键不同请求拒绝。返回 `snapshot_id:UUID, publication_epoch, watermark:int, expires_at, node_count:int, alias_count:int, content_hash`。默认 24 小时可读取；同键重试不延长有效期，过期后创建新 request_id。 |
| `GET /list?snapshot_id=…&cursor=…&limit=100` | 首页不传 cursor。limit=1..500，默认100个 item；返回 `snapshot_id,publication_epoch,watermark,node_count,alias_count,content_hash,items:[],next_cursor:string\|null,complete:bool`。最后逻辑页 complete=true 且 next_cursor=null；计数／hash 是全快照值。 |
| `GET /changes?publication_epoch=…&after=…&limit=100&cursor=…` | after 是该 epoch 已应用逻辑发布序。每次返回下一个完整逻辑批次，字段 `batch_id:UUID\|null,published_sequence:int,previous_sequence:int,transaction_groups:string[],items:[],alias_moves:[],batch_hash:sha256\|null,complete:bool,next_cursor:string\|null`；limit 是偏好的节点数（1..500），不可为满足 limit 拆源事务／闭包；超大批用下述传输分片。后续批使用已应用序再请求。 |
| `GET /parts/{part_id}` | 返回 `read_basis:"source_current",node,aliases:alias[],publication_status:"pending"\|"published",published_sequence:int\|null`。这是源当前完整状态，供人工冲突刷新／基线使用，不能直接改商城成员图、规范商品或组库存，也不能推进 feed checkpoint。身份修复须完整发布批／固定快照。 |
| `GET /digests?snapshot_id=…&cursor=…&limit=100` | 必须指定固定快照，或改用明确的 `publication_epoch=…&watermark=…`；后者仅支持服务端仍持有的固定版本，否则410。返回固定身份／水位、`segments:[{start_part_id:int,end_part_id_exclusive:int,node_count:int,alias_count:int,content_hash}],next_cursor,complete`。默认每段 ID 区间宽1000，首段 [1,1001)，别名按所属 part_id 入段。摘要规则见 §6。 |

快照来自已发布当前投影及同一 REPEATABLE READ 水位，先物化再提供 HTTP 页；分页不持数据库长事务。游标不可解释、不透明且绑定 client、epoch、snapshot／逻辑批、limit 和位置；禁止混用。源在翻页间修改不混入该快照。重复同页返回相同逻辑内容和摘要（请求关联号／生成时间可以不同）。全量先 staging 收齐所有页，核 count/hash、引用存在且无环，再切换；不能看到 merged 源页就先猜目标建商品。

无增量返回200：`batch_id:null,published_sequence=after,previous_sequence=after,transaction_groups:[],items:[],alias_moves:[],batch_hash:null,complete:true,next_cursor:null`，保留水位，不假造进度。非空批的 previous_sequence 必须等于调用方已应用水位，published_sequence 在同一 epoch 按持久发布顺序递增；重复已应用批 hash 相同视为幂等，异 hash／缺批／乱序停住，补取缺批，不能跳过坏批。

读取响应上限 **8 MiB（未压缩 UTF-8 JSON）**。普通 /list 页和 /changes 批超限时，服务端必须持久化其完整规范 JSON，并返回传输分片：`transfer_mode:"base64_fragments",publication_epoch,payload_id:UUID,part_no:int,part_count:int,part_hash:sha256,payload_hash:sha256,payload_fragment:string,complete:bool,next_cursor:string|null`，另携带§2的共同成功信封字段，并保留 snapshot_id 或 batch_id／published_sequence／previous_sequence。part_no 从1起，原始分片至多4 MiB，payload_fragment 为 RFC4648标准带 padding 的 Base64；按规范 JSON UTF-8 字节切片，允许切在字符中间，全部连接后才解码。part_hash 校验解码后原始片字节；payload_hash 校验连接后完整规范字节，/changes 还须校验逻辑 batch_hash。complete 只说明已发送最后片，不证明此前所有片已收妥；next_cursor 仅推进传输位置。

每片按 `(epoch,payload_id,part_no)` 幂等收妥，重复异 hash 拒绝；缺片、乱序、断线都只进 staging，收齐 part_count、整包验证并还原逻辑页／批后才使用 items。分片不拆逻辑事务，单节点的所有规格／别名也必须完整还原；不能以413永久阻塞合法大批或静默截短。/parts 当前值超8 MiB可明确413 `aggregate_too_large`，同步全量／增量路径必须支持上述可恢复分片。HTTP请求体上限64 KiB；命令超限整次拒绝。

已发布流至少保留30日；游标过期、所需历史水位被清理或 epoch 改变统一410 `cursor_expired`，details 指明 `reason:"retention_expired"|"epoch_changed"|"watermark_unavailable"`。快照过期410 `snapshot_expired`。消费方保留最后成功镜像，重新 staging 完整快照，并从其 epoch／watermark 接增量，不能径直跳到最新水位。默认30秒轮询、正常小批p95两分钟可见、五分钟提示延迟是实施验证目标；每日固定水位分段对账，未实测不称 SLA。

## 4. 发布与商城应用的原子边界

1. 源事务一次提交 PN、参数／别名、真实操作人审计、历史、版本、outbox。捕获须覆盖 INSERT／UPDATE／DELETE、Core/upsert/bulk、merge/unmerge／路径压缩、alias换属、仅参数删除；真实无变化不发布。outbox ID 与时间戳不作为提交序。
2. 单发布者同一一致快照内选全部已提交未发布事务组，递归展开“组→涉及PN→这些PN的全部可见未发布组→其全部PN”，并扩 canonical／alias旧新父至固定点；再物化完整聚合。T1改A、T2随后改A+B时，不能只取T1却读最新A而遗漏B。完整事务闭包、逻辑批、分片清单、当前投影、水位和具体日志标记在同一次发布事务持久化。甲先领outbox100晚提交、乙101早提交，二者都须最终发布。
3. 一致 seed 前先开捕获，seed后版本拒倒退重放，完成才 ready；全量只从发布投影读取，不把源实时值配旧水位。若需发布者会话锁，应先取锁再建立 RR 快照。
4. 商城网络读取在本地应用事务外。先收妥／校验，后在一个本地事务应用整个逻辑批的节点、别名、身份成员／商品投影及已应用水位，inbox 以 `(source_namespace,epoch,batch_id)`＋hash 去重。分别记录 received_sequence 与 applied_sequence；只收到片段不能显示已应用。失败整批回滚，保留 inbox 可重试。

两库无共同事务。命令主数据库已生效但商城绑定／应用失败时，查原回执并重试本地工作；不能重复建档、删除历史或恢复旧整库覆盖后续业务。回滚先关人工提交、停应用并保留捕获／批次／映射，使用仍认识身份组的兼容版本或前向修复；不倒拨版本／epoch或删数量事实。

## 5. 人工命令、鉴权与字段冲突

### 5.1 服务身份与实名用户

`catalog:read`、`catalog:submit`、`gallery:read` 使用独立凭据；旧 v1 Bearer 不扩权。凭据仅在后端运行环境注入，浏览器、模型、日志、离线样例均不接触。集成写入口仅供受控服务网络，跨网络 TLS／双向验证；商城本地 BFF 仍检查有效登录会话和 CSRF。

写请求与查重、回执查询都需 `catalog:submit`，并携带 `X-Catalog-Actor-Assertion`。**拟定签名格式**为 compact JWS `HS256`，受信 `kid` 定位商城独立提交签名密钥；payload 只含 `shop_subject,actor_mapping_id,body_hash,issued_at,nonce`：`shop_subject` 是商城 BFF 从**当前已认证会话**取得的商城用户ID，`actor_mapping_id` 由 BFF 按该会话**在服务端查出**，浏览器或请求参数提供的映射ID一律忽略，不得透传。issued_at 为UTC字符串，允许时钟偏差±300秒；nonce为至少128位随机值，按(client_id,nonce)唯一并保留10分钟。签名验证精确按其原始 JWS 字节；body_hash 按 §6计算。服务端实时从受信映射查有效实名 SysUser及当前编辑权限，并校验：映射属于本 client_id、映射登记的商城用户等于断言中的 `shop_subject`、请求体中的 `actor_mapping_id`（如有）与断言一致；任一不符按 401 invalid_actor_assertion 拒绝，防止冒用他人的特权映射；拒绝未映射、停用、共享回退身份与无权限用户，不信任商城／浏览器自报角色。不把读token、Agent或目录服务 actor 映射成人工编辑者。

POST的 body_hash 对应去掉 body_hash 的规范请求体；GET回执查询的断言 body_hash 对应 `{"method":"GET","path":"/api/integrations/catalog/v2/commands/<request_id>"}` 的规范字节。正常重试保留业务 request_id／内容但换新nonce与签名，nonce防重放独立于业务幂等。鉴权失败不暴露字段差异或原回执。

### 5.2 查重与请求 schema

`POST /duplicate-checks`：请求 `schema_version:2,request_id,correlation_id,actor_mapping_id,body_hash,source_namespace:"shop",source_pn_id:UUID,pn_std:string,identity_revision:int|null`；未绑定时 identity_revision=null。返回 `check_id:UUID,normalized_pn,identity_revision:int|null,exact:[],near:[]`，候选含 `part_id,pn_std,status,canonical_part_id,identity_revision`。trim+upper，不运行去V码规则；只复用既有人工查重，不联网，不授予服务token原生页面端点权限。检查只是预演，创建取锁后重查。

`POST /commands` 的必传字段：`schema_version:2,command_type:"patch"|"create"|"compensate",request_id,correlation_id,body_hash,actor_mapping_id,source_namespace:"shop",source_pn_id:UUID,part_id:int|null,base_revision:int|null,identity_revision:int|null,draft_revision:int,selected_fields:string[],field_baselines:[],operations:[],evidence_refs:[]`。evidence_refs为商城已有证据／导入ID字符串，不得夹带Cookie、密钥或原业务材料。最多50个操作，每个字段／规格键只一次；selected_fields必须与操作目标一一对应，不得重复或默认全选。

| 项目 | 精确定义 |
|---|---|
| 字段目标 | 普通字段用 `description,brand,category_major,category_minor,machine_or_part,unit`；规格用 `specs.<key>` 作为 selected_fields 与baseline.field，operation为 `field:"specs",key:<key>`。普通命令拒绝PN改名、merge、alias、制造商、营销／好坏件文案和任何业务状态，身份治理走既有专用流程。 |
| 规格白名单 | `part_type,capacity,interface,speed,rpm,cache,form_factor,generation,frequency,rank`。标准数值单位分别 capacity=GB、speed=Gbps、rpm=RPM、cache=MB、frequency=MHz；其余unit=null。set规格需 `value:string,unit:string|null,numeric_value:decimal_string|null`；校验展示值／数值／单位一致。未知键留商城候选，不静默丢弃。 |
| 操作 | `{"field":字段,"key"?:规格键,"op":"set"|"clear"|"remove","value"?:值,"unit"?:单位,"numeric_value"?:数值}`。set普通字段须非空字符串；清空须clear且无value，写null并设人工保护；remove仅用于删除规格键，clear仅普通可空字段。未选择／未传不是清空。 |
| 基线 | patch／compensate每个目标必有 `{"field":目标,"present":bool,"value":JSON值}`；普通列present=true、值可null；不存在规格为present=false,value=null；存在规格的value是完整spec对象。missing、null与空字符串不等价；以规范值直接比较。 |
| patch | part_id、base_revision、identity_revision必非null。先锁稳定父PN，重读身份及所选字段；仅非选字段变化可以合并；同选字段或身份变化整次409、零写。即使base_revision已前进，选字段基线与身份仍等于当前则允许。merged墓碑不能直接编辑，返回identity_conflict，让人重选，不静默改写canonical。 |
| create | part_id/base_revision/identity_revision=null，基线为空；另传 `pn_std,duplicate_check_id,force:bool,independent_confirmation:bool`。精确重名含merged墓碑409 pn_exists且force无效；近似候选409后人确认独立，以新request_id、force=true、independent_confirmation=true重提（仅跳近似）。人工建档沿既有manual、reviewed_at、字段锁规则，不强制待审；回传原商城source_pn_id绑定原商品。 |
| compensate | 除patch要求外携 `compensates_history_id`；用当前基线、人确认的逆向操作形成新版本，不倒拨旧版本，不覆盖之后修改。 |

描述先更新并重抽auto规格，再应用本次manual规格；manual保留，字段锁／字典ID／分类来源、参数、审计、历史、版本、outbox和生效回执同事务提交。父PN完整有序预锁遵循原外层导入／项目锁前缀，不能持父锁反向等待外层锁；网络不进入该事务。未知字段、单位／规格非法、权限或任一基线冲突，整条命令零业务写入。

### 5.3 幂等、回执与失败恢复

`(client_id,request_id)` 唯一绑定规范请求body_hash，服务端重算比对。同键同内容返回原结果，不新增资料、审计或建档；同键异内容409 `idempotency_conflict`，不得自动换键。进行中返回202回执或409 `command_in_progress`＋Retry-After，客户端按原键查询。终态rejected也保留请求hash和原错误；字段冲突后人工刷新并确认才可新request_id。鉴权／语法拒绝不伪造已接受命令，404回查后可按原键重放同内容。

`GET /commands/{request_id}` 同client及受信人授权范围查询；未找到404 `command_not_found`。回执含 `command_status:"in_progress"|"applied"|"rejected",source_pn_id,part_id:int|null,aggregate_revision:int|null,identity_revision:int|null,effective_values:object|null,audit_id:string|null,history_id:string|null,publication_status:"pending"|"published"|"not_applicable",published_sequence:int|null,error:object|null`。创建生效201、patch／compensate生效200、处理中202；拒绝用对应错误HTTP并返回统一error，GET终态rejected为200且error保留原code／details。应用失败不留成功回执。

published_sequence仅供观察，不是checkpoint。只在 source／part_id／identity_revision仍匹配且aggregate_revision不低于本地版本时，回执可刷新该PN资料；新create的首次绑定也要与完整feed融合，不能重复建商品。身份已变等待完整批。只有本地覆盖仍等于提交内容、draft_revision未变时，才可解除本次键的覆盖；后来编辑必须保留。页面分别显示“主数据库已生效／发布中”和“商城已应用”；网络超时保持结果未知，先查原request_id，不声称失败或两边已同步。

## 6. 规范编码与摘要算法（golden规则）

`C(x)`：递归按对象键的Unicode码点升序排序，数组按下述规则预排序，输出紧凑JSON（无空格、无末尾换行），UTF-8无BOM。不做Unicode归一、大小写／空白清洗；字符串保留原值。仅转义双引号、反斜线和控制字符；控制字符使用JSON短转义 `\b\t\n\f\r`，其余U+0000..001F用小写 `\u00xx`，其余Unicode标量直接UTF-8（不转义斜线）；拒绝重复键、未配对代理、浮点和非有限数。整数十进制无前导零；decimal字符串在进入C前规范为不带指数／加号、去无意义末尾零（`"1.500"→"1.5"`，`"-0.0"→"0"`）。业务展示value字符串不按Decimal清洗。

- `specs` 按key升序，locked_fields按字符串升序去重；nodes按part_id升序；aliases按alias_id升序；items先node再alias、各按其ID升序；transaction_groups按字符串升序；alias_moves按alias_id升序。命令 selected_fields、field_baselines按目标字段，operations按 `(field,key或空字符串)`、evidence_refs按字符串升序。重复目标拒绝，不靠排序掩盖。
- `H(x)=SHA256(C(x))`，结果小写hex。node.content_hash=H(移除自身content_hash后的完整node)。batch_hash=H(`{schema_version,source_namespace,publication_epoch,batch_id,published_sequence,previous_sequence,transaction_groups,items,alias_moves}`)。snapshot.content_hash=H(`{schema_version,source_namespace,nodes,aliases}`)，nodes保留已校验的content_hash、aliases不含deleted=true。segment同此对象，只取ID段内节点及其别名；空段数组也参与摘要。
- 请求body_hash=H(规范请求体去掉body_hash)；签名断言／HTTP头不进入它。别名完整项、null／false／空数组均进入摘要，不省略空值；node仅参数删除也改变hash。排除generated_at／request_id等传输信封、快照ID／水位、签名和短期URL；业务命令自身request_id／correlation_id属于幂等内容，仍在body_hash中。
- 分片part_hash／payload_hash直接对原始字节做SHA256，不再JSON编码。破坏hash覆盖范围、类型、排序或空值语义为breaking变更；同版本可选扩展仅可放成功响应信封顶层的 `extensions?`，它不进入node／batch／snapshot／segment hash的输入对象，也不得嵌套进这些对象或命令请求。无递归排除字段的隐含规则；extensions不授权新业务语义。

## 7. 错误与客户端动作

统一失败为 `{"error":{"code":string,"message":string,"retryable":bool,"request_id":UUID,"details":object}}`。details只含允许字段、版本和三方差异，不暴露SQL、凭据、签名URL查询或原始业务材料；message供人阅读，客户端按code分流。

| HTTP | code | 必须动作 |
|---|---|---|
| 401 | invalid_service_credential、invalid_actor_assertion | 停该凭据重试；不换读token尝试写。 |
| 403 | scope_denied、actor_unmapped、actor_permission_denied、field_forbidden | 保留草稿，显示权限缺口；不提升角色。 |
| 404 | integration_disabled、part_not_found、command_not_found | 明确未启用／不存在；缺原命令才按同键重放，不据故障自动建新PN。 |
| 409 | field_conflict、identity_conflict、idempotency_conflict、pn_exists、near_duplicate_confirmation_required、command_in_progress | 冲突整次零写、保留草稿；前两项刷新三方差异让人重选。硬重名不force；近似独立确认新键；进行中查原键。 |
| 410 | snapshot_expired、cursor_expired | 留旧镜像，重建完整快照，不跳水位。 |
| 413 | payload_too_large、aggregate_too_large | 明确上限；同步全量／增量用持久分片，不截断。 |
| 422 | invalid_field、invalid_spec、invalid_unit、invalid_cursor、unsupported_schema | 请求整次拒绝；不可理解批次停原水位待兼容，不部分采用。 |
| 429 | rate_limited | Retry-After内不重试，之后按原幂等键有限重试。 |
| 502／503／504 | upstream_unavailable、publication_not_ready | 读留最后资料；写结果未知先查原请求，不伪装未生效。 |

图库接口的具体错误码以 [gallery-v1.md](gallery-v1.md) 为准；两端必须保持其空、故障、分页过期和版本冲突语义。畸形数据、hash校验失败由消费方停批并记录校验错误，不向发布源确认已应用。

## 8. 离线 JSON golden 样例

以下全为合成资料。每块可独立交给JSON解析器，不需要数据库、网络、凭据或生产样本。§6的hash是规范字节的固定摘要，后续两仓测试应直接引用字面期望值，不能从各自生产代码生成期望值。

### 8.1 一个完整节点与当前别名

```json
{
  "part_id": 1,
  "pn_std": "DEMO-PN-A",
  "status": "active",
  "merged_into_id": null,
  "canonical_part_id": 1,
  "needs_review": true,
  "is_excluded": true,
  "machine_or_part": "备件",
  "description": "合成硬盘",
  "brand": "Demo",
  "category_major": "硬盘",
  "category_minor": "SSD",
  "unit": "块",
  "locked_fields": [
    "brand"
  ],
  "specs": [
    {
      "key": "capacity",
      "value": "960 GB",
      "unit": "GB",
      "numeric_value": "960",
      "source": "auto"
    }
  ],
  "aggregate_revision": 7,
  "identity_revision": 2,
  "content_hash": "cd61df703e596d1825651dbb30f2d24b8e2e5b53632d30462e51988cdb3081a7"
}
```

该node移除content_hash后 C 的UTF-8字节长度为 **435**，H为上述content_hash；`specs:[]`、description=null、source=manual、删除规格必须另加不同hash断言。全量页items按 §2 包装node及以下alias即可；三种未生效别名不得参与解析，deleted=true只可进增量。

```json
[
  {"alias_id":11,"pn_raw":"DEMO-OLD","part_id":1,"pn_std":"DEMO-PN-A","status":"active","deleted":false},
  {"alias_id":12,"pn_raw":"DEMO-PENDING","part_id":1,"pn_std":"DEMO-PN-A","status":"pending","deleted":false},
  {"alias_id":13,"pn_raw":"DEMO-REJECTED","part_id":1,"pn_std":"DEMO-PN-A","status":"rejected","deleted":false},
  {"alias_id":14,"pn_raw":"DEMO-LEGACY","part_id":1,"pn_std":"DEMO-PN-A","status":null,"deleted":false},
  {"alias_id":15,"pn_raw":"DEMO-DELETED","part_id":1,"pn_std":"DEMO-PN-A","status":"active","deleted":true}
]
```

### 8.2 快照回执与空增量

此空目录用于核验hash及空值，不代表运行中数据量。按同规则构造 snapshot 的空页：items=[]、complete=true、next_cursor=null，保持同snapshot_id／epoch／watermark；过期必须410，不能返回新版本空页。

```json
{"schema_version":2,"source_namespace":"itsp","request_id":"00000000-0000-4000-8000-000000000001","correlation_id":"00000000-0000-4000-8000-000000000002","generated_at":"2026-10-15T08:00:00Z","snapshot_id":"00000000-0000-4000-8000-000000000003","publication_epoch":"00000000-0000-4000-8000-000000000004","watermark":0,"expires_at":"2026-10-16T08:00:00Z","node_count":0,"alias_count":0,"content_hash":"cc504394eabdea844ec9aa5c696825dffd0cf087e071f7b7c343dc06ba7a10cd"}
```

```json
{"schema_version":2,"source_namespace":"itsp","request_id":"00000000-0000-4000-8000-000000000005","correlation_id":"00000000-0000-4000-8000-000000000002","generated_at":"2026-10-15T08:00:30Z","publication_epoch":"00000000-0000-4000-8000-000000000004","batch_id":null,"published_sequence":0,"previous_sequence":0,"transaction_groups":[],"items":[],"alias_moves":[],"batch_hash":null,"complete":true,"next_cursor":null}
```

### 8.3 非空合并闭包与别名删除

这是与8.2空目录互相独立的合成流：调用方已拥有A／B节点和旧别名，applied_sequence=0；A并B、alias11换属及alias15删除同属一个事务组。应用后A为merged、B为active；未见全部项前任何身份都不得切换。撤销合并必须同样发布完整恢复闭包而不是数量反向调拨。batch_hash覆盖 §6列出的逻辑字段，不覆盖传输信封。

```json
{"schema_version":2,"source_namespace":"itsp","publication_epoch":"00000000-0000-4000-8000-000000000004","batch_id":"00000000-0000-4000-8000-000000000020","published_sequence":1,"previous_sequence":0,"transaction_groups":["demo-merge-1"],"items":[{"kind":"node","node":{"part_id":1,"pn_std":"DEMO-PN-A","status":"merged","merged_into_id":2,"canonical_part_id":2,"needs_review":true,"is_excluded":true,"machine_or_part":"备件","description":"合成硬盘","brand":"Demo","category_major":"硬盘","category_minor":"SSD","unit":"块","locked_fields":["brand"],"specs":[{"key":"capacity","value":"960 GB","unit":"GB","numeric_value":"960","source":"auto"}],"aggregate_revision":8,"identity_revision":3,"content_hash":"141c272be2d7cc27ffccd6e51e365e10e07a4317a0e8929564573cb920c43d5e"}},{"kind":"node","node":{"part_id":2,"pn_std":"DEMO-PN-B","status":"active","merged_into_id":null,"canonical_part_id":2,"needs_review":false,"is_excluded":false,"machine_or_part":"备件","description":"合成硬盘","brand":"Demo","category_major":"硬盘","category_minor":"SSD","unit":"块","locked_fields":["brand"],"specs":[{"key":"capacity","value":"960 GB","unit":"GB","numeric_value":"960","source":"auto"}],"aggregate_revision":4,"identity_revision":2,"content_hash":"954d1c04028c6a3f7384b4cfe10a09819cb80f307a742898fbed6d9973f85ca0"}},{"kind":"alias","alias":{"alias_id":11,"pn_raw":"DEMO-OLD","part_id":2,"pn_std":"DEMO-PN-B","status":"active","deleted":false}},{"kind":"alias","alias":{"alias_id":15,"pn_raw":"DEMO-DELETED","part_id":1,"pn_std":"DEMO-PN-A","status":"active","deleted":true}}],"alias_moves":[{"alias_id":11,"old_part_id":1,"new_part_id":2}],"batch_hash":"2e98be24ae1f13517c406a12cdd2b522bd54214b8c3e0caca75c218b2a75dd1c","request_id":"00000000-0000-4000-8000-000000000021","correlation_id":"00000000-0000-4000-8000-000000000002","generated_at":"2026-10-15T08:00:45Z","complete":true,"next_cursor":null}
```

同一合成流的下一批撤销上述合并；previous_sequence=1，只能在上一整批已应用后应用。应用后A／B分别为active根，alias11回A，alias15仍删除（未传不是复活）；原商城库存桶／原图不变。

```json
{"schema_version":2,"source_namespace":"itsp","publication_epoch":"00000000-0000-4000-8000-000000000004","batch_id":"00000000-0000-4000-8000-000000000022","published_sequence":2,"previous_sequence":1,"transaction_groups":["demo-unmerge-1"],"items":[{"kind":"node","node":{"part_id":1,"pn_std":"DEMO-PN-A","status":"active","merged_into_id":null,"canonical_part_id":1,"needs_review":true,"is_excluded":true,"machine_or_part":"备件","description":"合成硬盘","brand":"Demo","category_major":"硬盘","category_minor":"SSD","unit":"块","locked_fields":["brand"],"specs":[{"key":"capacity","value":"960 GB","unit":"GB","numeric_value":"960","source":"auto"}],"aggregate_revision":9,"identity_revision":4,"content_hash":"0bc6d71cc8d9c8be12799231a598320aaa1dad5361a0db50d5b3e0d1080c0193"}},{"kind":"node","node":{"part_id":2,"pn_std":"DEMO-PN-B","status":"active","merged_into_id":null,"canonical_part_id":2,"needs_review":false,"is_excluded":false,"machine_or_part":"备件","description":"合成硬盘","brand":"Demo","category_major":"硬盘","category_minor":"SSD","unit":"块","locked_fields":["brand"],"specs":[{"key":"capacity","value":"960 GB","unit":"GB","numeric_value":"960","source":"auto"}],"aggregate_revision":5,"identity_revision":3,"content_hash":"c556a43a1f5b6aa9d5d9af601ddf155e7ca31739aafa88d2f3084cccd39b4387"}},{"kind":"alias","alias":{"alias_id":11,"pn_raw":"DEMO-OLD","part_id":1,"pn_std":"DEMO-PN-A","status":"active","deleted":false}}],"alias_moves":[{"alias_id":11,"old_part_id":2,"new_part_id":1}],"batch_hash":"ad1b352698c46b8dc49faa37c1baa1a69f01db2268ae7e8497f9e3504f272308","request_id":"00000000-0000-4000-8000-000000000023","correlation_id":"00000000-0000-4000-8000-000000000002","generated_at":"2026-10-15T08:00:50Z","complete":true,"next_cursor":null}
```

### 8.4 选择清空与逐键删除的人工命令

请求不带签名／凭据；服务端的受信映射不是此字符串自我声明可创建的授权。示例一次清空品牌、删除capacity，未选description不得被清空；body_hash为真实固定摘要。

```json
{
  "schema_version": 2,
  "command_type": "patch",
  "request_id": "00000000-0000-4000-8000-000000000010",
  "correlation_id": "00000000-0000-4000-8000-000000000002",
  "actor_mapping_id": "demo-mapping-1",
  "source_namespace": "shop",
  "source_pn_id": "00000000-0000-4000-8000-000000000011",
  "part_id": 1,
  "base_revision": 7,
  "identity_revision": 2,
  "draft_revision": 3,
  "selected_fields": [
    "brand",
    "specs.capacity"
  ],
  "field_baselines": [
    {
      "field": "brand",
      "present": true,
      "value": "Demo"
    },
    {
      "field": "specs.capacity",
      "present": true,
      "value": {
        "key": "capacity",
        "value": "960 GB",
        "unit": "GB",
        "numeric_value": "960",
        "source": "auto"
      }
    }
  ],
  "operations": [
    {
      "field": "brand",
      "op": "clear"
    },
    {
      "field": "specs",
      "key": "capacity",
      "op": "remove"
    }
  ],
  "evidence_refs": [],
  "body_hash": "d072566a3631835bda58226faf6b9955bf79db88354cb4b5607fb66116000dc4"
}
```

同键同内容的200回执示例（完整有效值仅含选中字段，spec删除以present=false表示；真实人、前后值等完整审计留服务端）：

```json
{"schema_version":2,"source_namespace":"itsp","request_id":"00000000-0000-4000-8000-000000000010","correlation_id":"00000000-0000-4000-8000-000000000002","generated_at":"2026-10-15T08:01:00Z","command_status":"applied","source_pn_id":"00000000-0000-4000-8000-000000000011","part_id":1,"aggregate_revision":8,"identity_revision":2,"effective_values":{"brand":null,"specs.capacity":{"present":false,"value":null}},"audit_id":"demo-audit-1","history_id":"demo-history-1","publication_status":"pending","published_sequence":null,"error":null}
```

若服务端同选字段已由Demo改为Other，整次409（包括capacity删除也不得生效），三方差异如下。若仅description变动且身份和所选基线未变，应允许该命令生效并保留新description。

```json
{"error":{"code":"field_conflict","message":"所选字段已变化，请重新选择后确认","retryable":false,"request_id":"00000000-0000-4000-8000-000000000010","details":{"part_id":1,"base_revision":7,"current_revision":8,"identity_revision":2,"conflicts":[{"field":"brand","baseline":{"present":true,"value":"Demo"},"current":{"present":true,"value":"Other"},"proposed":{"present":true,"value":null}}]}}}
```

### 8.5 分片收妥与旧回执晚到

下列数组是两个独立HTTP分片响应；为离线练习将小型空页强制切为2片，实际超8MiB页／批采用相同规则。按part_no连接Base64解码后的字节，应还原8.2同一空快照的逻辑页（不含expires_at，含items／next_cursor／complete）。只先收到第2片时，即使complete=true也不得应用；重复第1片不重复计数；片hash不符或缺片水位保持0。

```json
[{"transfer_mode":"base64_fragments","publication_epoch":"00000000-0000-4000-8000-000000000004","snapshot_id":"00000000-0000-4000-8000-000000000003","payload_id":"00000000-0000-4000-8000-000000000030","part_no":1,"part_count":2,"part_hash":"a4c2cd7eba9219afd27d2792d0d626a5bd5d82d7f8072ec4bf884c3ddbf8fb8d","payload_hash":"b872723b239f8a68f7675839cd5644299408e58e2ba46a69e2257f10633df321","payload_fragment":"eyJhbGlhc19jb3VudCI6MCwiY29tcGxldGUiOnRydWUsImNvbnRlbnRfaGFzaCI6ImNjNTA0Mzk0ZWFiZGVhODQ0ZWM5YWE1YzY5NjgyNWRmZmQwY2YwODdlMDcxZjdiN2MzNDNkYzA2YmE3YTEwY2QiLCJjb3JyZWxhdGlvbl9pZCI6IjAwMDAwMDAwLTAwMDAtNDAwMC04MDAwLTAwMDAwMDAwMDAwMiIsImdlbmVyYXRlZF9hdCI6IjIwMjYtMTAtMTVUMDg=","complete":false,"next_cursor":"demo-fragment-cursor-2","schema_version":2,"source_namespace":"itsp","request_id":"00000000-0000-4000-8000-000000000031","correlation_id":"00000000-0000-4000-8000-000000000002","generated_at":"2026-10-15T08:00:00Z"},{"transfer_mode":"base64_fragments","publication_epoch":"00000000-0000-4000-8000-000000000004","snapshot_id":"00000000-0000-4000-8000-000000000003","payload_id":"00000000-0000-4000-8000-000000000030","part_no":2,"part_count":2,"part_hash":"71e97bcb368f6f2e998681a780895406805c1a15921a12df375d42334aa1f1f5","payload_hash":"b872723b239f8a68f7675839cd5644299408e58e2ba46a69e2257f10633df321","payload_fragment":"OjAwOjAwWiIsIml0ZW1zIjpbXSwibmV4dF9jdXJzb3IiOm51bGwsIm5vZGVfY291bnQiOjAsInB1YmxpY2F0aW9uX2Vwb2NoIjoiMDAwMDAwMDAtMDAwMC00MDAwLTgwMDAtMDAwMDAwMDAwMDA0IiwicmVxdWVzdF9pZCI6IjAwMDAwMDAwLTAwMDAtNDAwMC04MDAwLTAwMDAwMDAwMDAwMSIsInNjaGVtYV92ZXJzaW9uIjoyLCJzbmFwc2hvdF9pZCI6IjAwMDAwMDAwLTAwMDAtNDAwMC04MDAwLTAwMDAwMDAwMDAwMyIsInNvdXJjZV9uYW1lc3BhY2UiOiJpdHNwIiwid2F0ZXJtYXJrIjowfQ==","complete":true,"next_cursor":null,"schema_version":2,"source_namespace":"itsp","request_id":"00000000-0000-4000-8000-000000000032","correlation_id":"00000000-0000-4000-8000-000000000002","generated_at":"2026-10-15T08:00:00Z"}]
```

旧回执恢复断言：8.4回执aggregate_revision=8／identity_revision=2，若商城feed已到aggregate_revision=9或identity_revision=3，回执仅存历史，不覆盖资料／身份、不推进checkpoint；即使回执比feed先到，也只安全刷新匹配身份的选中资料，之后完整批正常应用。draft_revision已从3变4时不得清掉后改输入。相同request_id更改任一operation必须409 idempotency_conflict。

### 8.6 hash及兼容性独立校验

`C({"aliases":[],"nodes":[],"schema_version":2,"source_namespace":"itsp"})` 的精确字节（无换行）为：

```text
{"aliases":[],"nodes":[],"schema_version":2,"source_namespace":"itsp"}
```

其SHA256为 `cc504394eabdea844ec9aa5c696825dffd0cf087e071f7b7c343dc06ba7a10cd`。参数规范数值 `"1.500"` 归一成 `"1.5"` 后摘要相同；value展示文字 `"1.500 TB"` 不归一。把null换成缺字段、把deleted=false改true都必须产生不同摘要。

## 9. 兼容矩阵与后续验收闸门

| 生产者／消费者 | 可用能力 | 兼容处理 |
|---|---|---|
| 现有resale v1／现有v1客户端 | `/api/integrations/resale/parts/{resolve,snapshots,price-evidence,search}` 原camelCase、schemaVersion=1、认证／错误 | 完全保留；不往旧响应塞v2或用旧读token写；v2关停不影响旧链路。 |
| catalog v2／尚未升级的商城 | v2独立URL与凭据 | 不启新消费；v1继续。 |
| 本文v2.0-draft双方模拟／实现 | 本文节点、快照、批次、哈希、命令及错误 | 同副本、golden与真实PG契约通过后才能逐项开关；本文不是已实现OpenAPI。 |
| schema_version>2／v2消费者 | 不受支持 | 停批、告警、留原已应用水位；先规范升级，再消费者与生产者兼容启用。 |
| v2已启用身份组／回退构建 | 旧资料可读、库存与图库照常，提交／应用可关闭 | 只用仍认识身份组的兼容版；不能直接回到按单PN读库存的旧版。 |
| catalog v2／gallery v1 | 独立协议及scope | gallery失败不阻塞目录／资料编辑；catalog成功不证明图库可读。 |

后续实现验收至少包含：快照中途增改／合并仍固定hash；源100晚提交与101早提交均发布；T1改A／T2改A+B固定点不漏B；merge/unmerge／alias换属闭包和删除；超大规格批分片缺失／乱序／重复／错hash均零应用零水位推进；epoch／过期全量恢复；v1全DTO不变；读token、无映射／停用／无权限人拒写；同字段整次冲突、异字段合并；显式clear/remove；同键异内容拒绝；主数据库成功而商城失败及feed先到／回执晚到不重复建品、不盖新草稿；无点击零AI；好坏库存及原图／原件各自守恒。浏览器调试不代替用户对候选实际点按验收。
