# 售前询价接口契约

路径统一挂在 `/api/presales`。所有接口要求在职实名账号和 `page_presales`，不接受共享口令身份。
服务仅 flush，HTTP 接口提交；业务时间线和访问审计与写动作同事务。未知输入字段返回 422。

## 路由

| 方法 | 路径 | 行为与动作权限 |
|---|---|---|
| GET | `/customers?q=&limit=20` | 客户搜索候选；需 `action_presales_inquire`，且 `data_customer` 字段组可见，否则 403；遵守 `own_customers_only` |
| GET | `/suppliers?q=&limit=20` | 供应商搜索候选；需 `action_presales_quote` 或 `action_presales_offer`，且 `data_supplier` 字段组可见，否则 403 |
| GET | `/inquiries?kind=&scope=mine\|all&status=&page=&page_size=` | 仅返回有权查看的单据；mine 包括本人发起或本人处理，all 包括可见共享单 |
| POST | `/inquiries` | 草稿建单；销售单需 `action_presales_inquire`，主动报价需 `action_presales_offer` |
| GET | `/inquiries/{inquiry_id}` | 单头、活动行、受控报价、本人可见待办、字段值与按 PN 空白模板 |
| PATCH | `/inquiries/{inquiry_id}` | 草稿或 revising 可修改；lines 为完整活动行集合，按 line_key 对齐，缺行逻辑删除 |
| DELETE | `/inquiries/{inquiry_id}` | 仅草稿，204；保守软关单并保留历史 |
| POST | `/inquiries/{inquiry_id}/submit` | 销售草稿提交；多处理人或公共池，没有待询价行时不派单 |
| POST | `/inquiries/{inquiry_id}/withdraw` | 撤回为 revising，暂停未结束待办 |
| POST | `/inquiries/{inquiry_id}/resubmit` | 按 line_key 比较上一提交版，报价 stale、恢复或重开待办，revision 加一 |
| POST | `/inquiries/{inquiry_id}/lines` | 追加新行，不需撤回；body 为 expected_version、lines |
| POST | `/inquiries/{inquiry_id}/close` | outcome=won/lost/cancelled，won_offer_id 可选且必须属于本单或本单引用／沿用的报价；记录采纳版本并结束未完成待办 |
| POST | `/inquiries/{inquiry_id}/copy` | 返回新草稿、新编号和新 line_key；保留原生字段及可见性，不复制报价、引用、成交、字段值 |
| POST | `/inquiries/{inquiry_id}/lines/{line_id}/offers` | 创建或保存报价草稿；采购需 `action_presales_quote` 且为本单处理人，主动报价需 `action_presales_offer` |
| POST | `/inquiries/{inquiry_id}/reply` | 独立回复；所有未引用活动行必须报新价、沿用或不报，锁定本人草稿、发布快照并完成本人待办 |
| POST | `/inquiries/{inquiry_id}/publish` | 主动报价发布；notice_user_ids 可定向通知在职且有 page_presales 的账号 |
| PATCH | `/offers/{offer_id}` | 发布报价更新并续期、追加完整快照；报价本人或主管，需对应 quote/offer 动作权限 |
| POST | `/offers/{offer_id}/withdraw` | 立即下架，reason 必填且非空白；追加撤销快照 |
| GET | `/offers/{offer_id}/revisions` | 仅发起人、报价本人、主管；历史和当前可见性取交集，屏蔽作者旁路和私密字段 |
| GET | `/lines/{line_id}/part-check?pn_std=` | 待建档查重，复用 master_edit.find_near_duplicates |
| POST | `/lines/{line_id}/bind-part` | body 为 part_id、expected_version；绑定已有 PN，仅可绑定一次 |
| POST | `/lines/{line_id}/create-part` | body 为 pn_std、主数据字段、expected_version；查重有候选时拒绝新建，要求绑定 |
| GET | `/library?part_ids=1,2` | 批量返回 `{part_id: [offer]}`；每 PN 每报价人一条最新有效主报价，其他可见有效报价置于 more；空列表表示无可见有效报价 |
| GET | `/library/search?q=&limit=` | M1 实时检索，M8 过滤后的有效报价加权，每个候选附去重后的 offers、offer_count（报价人数） |
| POST | `/reference` | 采购参考：part_ids 或 inquiry_id；返回 M3 reference、offers、gallery=[{part_id}]；指定 inquiry_id 时沿用候选同时满足查看者与单据所有人的 M8／字段组权限 |

建档两种写动作以及查重均要求 `action_presales_bind_part` 和 `page_master_data`，且必须为处理人、
主动报价发起人或主管。公共池先领单。主数据字段包括 description、brand、manufacturer、summary、
category_major、category_minor、machine_or_part、unit，以及 `spec_updates`；主数据不随询价自动合并。

按 [D-0045](../decisions/0025-售前PN建档与操作诊断.md)，询价/主动报价编辑器还可从 PN 联想入口显式打开主数据建档。
该入口复用 `GET /parts/master/check`、`GET /parts/master/categories` 和 `POST /parts/master`，不依赖已保存的询价行；
要求主数据权限及实名身份。建档回执的 `id` 与 `pn_std` 加入当前草稿，保存/提交单据仍是后续独立动作。
已提交询价行继续使用本节的原子建档/绑定入口，要求原有售前动作权限及处理人身份。

`spec_updates` 为 `{参数键: {value: 字符串, unit: 字符串或null, numeric_value: 十进制字符串或null}}`。
标准参数键、标签、单位来自 `/parts/master/categories` 的 `spec_fields` 数组；数值遵守原规格单位校验。
自由文本参数以 `custom:<名称>` 存储（名称最多25字符，值最多128字符，unit/numeric_value均为null），
在候选中显示参数名称，不展示内部前缀。两入口都将规格、主数据及审计原子提交；失败不留下半份档案。
新建类别须属于主数据字典，二级类别须属于所选一级类别。

客户、供应商候选都直接返回 `[{id, name}]`（无分页包装），id 为维度表整数主键，
name 优先使用非空归一化名称、回退原始名称；limit 默认 20、范围 1–100，q 最长 256 字符。
q 为空时返回权限范围内的候选，非空时对原始名称、归一化名称做模糊匹配。
既有销售接口没有独立的客户归属关系，因此受 `own_customers_only` 限制的账号只返回
`f_sales_order.salesperson` 匹配账号 `salesperson_name` 的客户，或本人询价中用过的客户；
未绑定 salesperson_name 时仅返回本人询价中用过的客户。
客户与供应商字段组的可见性均复用 `is_field_hidden`，与 `apply_field_visibility` 使用同一判定来源。
客户端使用售前专用候选接口，不复用分析页 options；没有对应字段权限时不展示客户或供应商选择器。

## 输入与版本

创建单据接受 kind、customer_id、notes、lines、assignee_ids、offer_audience、notes_audience、field_values。
行接受 line_key、part_id、pn_raw、description、qty、require_exact_pn、ref_offer_id、notes、notes_audience、field_values。
未建档行可仅传 pn_raw。行 qty 为正数、最多三位小数。每次 HTTP 提交最多 200 行。

报价接受 unit_price、is_tax_inclusive、no_stock、qty_available、stock_location、lead_time、source_key、supplier_id、
notes、supplier_audience、reporter_audience、notes_audience。金额是 Money(14,2)，不转换税基。
按 [D-0046](../decisions/0026-售前报价长期有效与主动报价简化.md)，报价不设有效期：不再接受 validity_days，
响应不再返回 valid_until / validity_days；`valid_from` 即发布或最近一次更新的时间。
实时有效判断为「已发布、未标无货、有单价且 `valid_from <= now`」，不看时间截止；直到更新、撤回或标为失效前一直有效。
旧 Excel 中的「有效天数」列按未知列忽略。

除创建外，所有业务写动作要求 expected_version。询价动作校验询价 version；绑定/建档校验行 version；
更新、撤销、指定 offer_id 保存草稿校验报价 version；新报价草稿校验询价 version。
同 PN、同货源的草稿已经存在时，未带 offer_id 的旧请求返回 409 并给出目标报价及版本，
调用方使用该 offer_id 和报价版本编辑，避免旧询价版本覆盖已保存草稿。
reply 可附 work_item_id、expected_work_item_version，未提供时服务读取并锁定当前本人待办。
报价草稿与独立回复不递增询价 version，避免互不相关的采购回复互相冲突。询价行、报价各自递增版本。
版本冲突返回 409，响应 detail 含 code=version_conflict、message 与当前 version；业务状态非法 422，无权 403，不存在 404。
售前服务错误的 `detail` 保留既有数据并补充稳定 `code`；近似 PN 为 `near_duplicates`，已绑定为 `already_bound`，
已有报价草稿为 `existing_draft`，权限为 `permission_denied`。客户端不将所有409解释为版本冲突。
售前、主数据建档、PN 联想及待办相关 HTTP 响应带服务端生成的 `X-Request-ID`，失败日志使用同一标识。
前端可复制错误码、HTTP状态、方法、路径和请求标识；诊断内容不含查询参数、请求体、凭据和原始响应。

发布和回复支持 `on_existing=new|update`、`existing_offer_id`，或按草稿 ID 指定的
`existing_choices={draft_offer_id: {on_existing, existing_offer_id?}}`。同采购对同 PN 已有有效报价时，
必须明确选择另报一条或更新某条；不按货源或受众自动归并。缺选择返回 409，detail 包含
`code=existing_offer`、`draft_offer_id`、`line_id`、经 M8 过滤的 `existing_offers`。
多条候选时 update 必须指定目标；整批预检通过后才写入，不允许一批多个草稿更新同一目标。
同批首次发布的多个草稿分别保留。

`reply.line_responses=[{line_id, mode: quote|reuse|skip, ref_offer_id?, reason?}]`：
quote 对应本人新报价草稿；reuse 挂接当前有效报价；skip 表示无货／不报且必须提供非空白原因。
沿用目标必须既对执行采购可见，也对询价单所有人 `created_by` 按报价库完全相同的 M8 与字段组规则可见，
否则 422。沿用不复制价格、不追加报价历史，只保存报价 ID 和当时版本。主动无货报价也必须在 notes 填原因。

受众输入对象为 `{mode: public|private|include|exclude, user_ids: [username], group_ids: [id]}`。
user_ids 使用账号字符串；assignee_ids、notice_user_ids 使用账号整数主键。供应商默认 private，报价人、备注默认 public。
私密内容本人和主管可见，同时继续满足 data_supplier/data_customer 字段组。
报价返回 reporter={id,name}，隐藏时整个删除，不返回 created_by/work_item_id 或规则 ID 作为身份旁路。
其他销售的单据响应始终不包含 customer_id；共享摘要、角标、库、引用、时间线都在服务器过滤。

受众回读对象为 `{mode, users: [{id, name}], groups: [{id, name}]}`，其中 users.id 是账号字符串，
groups.id 是分组整数主键。回读不返回规则 ID 或分组成员名单；未显式设置规则时回读当前生效的默认规则。
仅单据作者本人或 bypass（`action_presales_supervise`、老板、管理员）能在询价详情读取
单头 `offer_audience`、`notes_audience` 以及行 `notes_audience`；仅报价作者本人或 bypass 能在报价视图读取
`supplier_audience`、`reporter_audience`、`notes_audience`。询价发起人不因此成为其采购报价的作者。
其他销售和采购的响应完全省略这些规则字段，字段值仍按原有 M8 和字段组权限过滤。
编辑受众时用回读的用户和分组名称回显，用户未修改规则就省略对应输入字段，保留已有设置。

字段值为 `[{field_def_id 或 new_name, value, rule?}]`，遵循 M7 增量保存；未传项保留，value=null 清空。
主体为 presales_inquiry，单头 subject_id=`inquiry:{id}`，行 subject_id=`line:{id}`；anchor=`part:{part_id}`。
新行自动带出的 field_template 只有定义，不带历史值。
售前读取 M7 字段值时显式启用 `read_values(..., include_rules=True)`；其他调用默认不回读规则。
仅在字段值作者（最后填写人）本人或 bypass 读取时，为可见字段值额外返回同格式的 `rule`；
其他查看者不返回 `rule`，被隐藏的字段值整项不返回。规则取字段值覆盖值，未覆盖则取字段定义的默认值。

## 模块与历史

M5 注册 presales_inquiry（池 action_presales_quote，主管 action_presales_supervise）及 presales_offer_notice。
通用待办 complete 也校验逐行已回复，不能经通用接口绕过报价完整性。时间线使用现有 subjects/events 接口。
团队字段池和公共分组接受售前主管。业务层不直接查询或更新通用模块的表。

每次发布、更新、撤销、stale 都增加 revision_no 并追加完整快照；数据库触发器拒绝 UPDATE/DELETE。
明确 update 时保留报价 ID，完整覆盖最新价格、货源和受众，归属迁移到本次单据和行；
原行保存钉版本的稳定关联，不能通过旧归属获得新的受限价格。关单后只能复制，不能更新该单报价。

### 钉住的报价版本（r7）

销售引用保存 `line.ref_offer_id` 和 `line.ref_offer_revision_no`；采购沿用保存
`line_response.ref_offer_id` 和 `line_response.ref_offer_revision_no`；成交保存
`won_offer_id` 和 `won_offer_revision_no`；跨单更新的原行关联保存 `offer_revision_no`。
版本由服务端确定，客户端不能自行提交版本号。
价格、含税口径、数量、货源及受众均读取所钉版本的 `presales_offer_revision.snapshot`，
不取当前报价覆盖。新快照保存当时整单准入所需信息；内部准入信息不出现在业务响应中。
单据详情返回行级 `ref_offer`、跨单原行级 `linked_offers`、成交级 `won_offer` 和沿用结果中的 `offer` 快照；
不可读快照返回 null 或省略 offer，不用当前报价补全。
同一报价 ID 可以在不同行钉住不同版本；展示时优先使用每行的 linked_offers，不能用顶层 offers 按 ID 反查替代。

快照可见性仍通过 M8 和查看者当前字段组权限过滤；分组成员变更仍按 M8 的实时成员规则生效。
当前版本较新且查看者对当前版本也有权限时，快照视图额外包含
`latest={unit_price, is_tax_inclusive, revision_no}`；否则整个省略 latest，不能透露新价或更新版本号。
前端以 latest 显示「该报价已更新为 ¥x」。钉住版本直到原报价下一次更新前有效（D-0046）：原报价改价、改含税口径、
标无货或标为失效后 `expired=true`；只改备注、货源等其他字段或可见范围不算取代；撤销另标「原报价已撤销」。
快照视图返回 `expired` 与 `original_withdrawn`，行及沿用结果同步返回这两个标志。
沿用行显示原报价人和报价时间；`reporter_label` 依据所有人权限返回姓名或「其他采购」，不泄露身份。

新增四列均可空。r7 之前的旧关联无法可靠反推当时版本，迁移不猜测、不把当前版本回填为历史版本，
也不改写只追加历史；缺少钉版本的旧关联待主管核对。
缺少整单准入元信息的旧格式历史不可用于新引用、沿用或成交采纳，保守返回 422；原报价人更新后可生成完整的新快照。
已有 NULL 版本引用不会因编辑备注自动绑定当前版本，需要明确取消后重新选择。
原行已完成的沿用在追加其他行、再次回复或显式重发相同选择时保持原版本，不因再次回复而续期。
成交按关单当时版本记录；若引用版本已更新，前端采纳选项使用获授权的 latest 当前价、税基和版本提示。

### 报价库分组与排序（r7）

先做 M8 和字段组过滤，再按 `(part_id, created_by)` 分组，主列表选最新有效报价；
同人的其他有效报价放进该条 `more`（不递归分组）。分组使用服务内部身份，隐藏 reporter 不会妨碍去重，
也不能借分组键返回隐藏的人员 ID。报价库与处理页沿用候选使用相同结构。
主列表和 more 均先按含税、未税、税基未知分区，再在区内按价格升序；不换算税基，也不跨税基比价。
M1 角标的最低价取主列表首条，人数取主列表长度。

报价日缓存可通过 `python -m app.services.presales.daily --rebuild [--since YYYY-MM-DD]` 重建。
取上海变更日每条报价最后快照，按含税、未税、未声明税基分开聚合，不推算价格，不跨日补齐。
仓库没有通用接口契约生成脚本；本契约与 routes.tsv 登记全部售前路由。
