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
| POST | `/inquiries/{inquiry_id}/close` | outcome=won/lost/cancelled，won_offer_id 可选且必须是本单报价；结束未完成待办 |
| POST | `/inquiries/{inquiry_id}/copy` | 返回新草稿、新编号和新 line_key；保留原生字段及可见性，不复制报价、引用、成交、字段值 |
| POST | `/inquiries/{inquiry_id}/lines/{line_id}/offers` | 创建或保存报价草稿；采购需 `action_presales_quote` 且为本单处理人，主动报价需 `action_presales_offer` |
| POST | `/inquiries/{inquiry_id}/reply` | 独立回复；所有未引用活动行有报价或无货，锁定本人草稿、发布快照并完成本人待办 |
| POST | `/inquiries/{inquiry_id}/publish` | 主动报价发布；notice_user_ids 可定向通知在职且有 page_presales 的账号 |
| PATCH | `/offers/{offer_id}` | 发布报价更新并续期、追加完整快照；报价本人或主管，需对应 quote/offer 动作权限 |
| POST | `/offers/{offer_id}/withdraw` | 立即下架，reason 必填且非空白；追加撤销快照 |
| GET | `/offers/{offer_id}/revisions` | 仅发起人、报价本人、主管；历史和当前可见性取交集，屏蔽作者旁路和私密字段 |
| GET | `/lines/{line_id}/part-check?pn_std=` | 待建档查重，复用 master_edit.find_near_duplicates |
| POST | `/lines/{line_id}/bind-part` | body 为 part_id、expected_version；绑定已有 PN，仅可绑定一次 |
| POST | `/lines/{line_id}/create-part` | body 为 pn_std、主数据字段、expected_version；查重有候选时拒绝新建，要求绑定 |
| GET | `/library?part_ids=1,2` | 批量返回 `{part_id: [offer]}`，空列表表示无可见有效报价 |
| GET | `/library/search?q=&limit=` | M1 实时检索，M8 过滤后的有效报价加权，每个候选附 offers、offer_count |
| POST | `/reference` | 采购参考：part_ids 或 inquiry_id；返回 M3 reference、offers、gallery=[{part_id}] |

建档两种写动作以及查重均要求 `action_presales_bind_part` 和 `page_master_data`，且必须为处理人、
主动报价发起人或主管。公共池先领单。主数据字段包括 description、brand、manufacturer、summary、
category_major、category_minor、machine_or_part、unit；主数据不随询价自动合并。

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
validity_days、notes、supplier_audience、reporter_audience、notes_audience。金额是 Money(14,2)，不转换税基。
validity_days 为 1–36500 的自然日数；截止时刻为上海发布日期加 N−1 天的 23:59:59.999999，存 UTC。
实时有效判断为 `valid_from <= now < valid_until`，截止微秒本身无效。

除创建外，所有业务写动作要求 expected_version。询价动作校验询价 version；绑定/建档校验行 version；
更新、撤销、指定 offer_id 保存草稿校验报价 version；新报价草稿校验询价 version。
同 PN、同货源的草稿已经存在时，未带 offer_id 的旧请求返回 409 并给出目标报价及版本，
调用方使用该 offer_id 和报价版本编辑，避免旧询价版本覆盖已保存草稿。
reply 可附 work_item_id、expected_work_item_version，未提供时服务读取并锁定当前本人待办。
报价草稿与独立回复不递增询价 version，避免互不相关的采购回复互相冲突。询价行、报价各自递增版本。
版本冲突返回 409，响应 detail 含 message 与当前 version；业务状态非法 422，无权 403，不存在 404。

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
同人同 PN 同货源的主动报价再次发布时更新原报价，原来源单和行保留，新行 ref_offer_id 指向原报价。
销售询价按本单本行更新，以保持 won_offer_id 的本单约束。关单后只能复制，报价不能再改。

报价日缓存可通过 `python -m app.services.presales.daily --rebuild [--since YYYY-MM-DD]` 重建。
取上海变更日每条报价最后快照，按含税、未税、未声明税基分开聚合，不推算价格，不跨日补齐。
仓库没有通用接口契约生成脚本；本契约与 routes.tsv 登记全部售前路由。
