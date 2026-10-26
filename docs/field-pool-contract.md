# M7 字段池契约

业务层通过 `app.services.field_pool` 调用本模块。主体为 `Subject(subject_type, subject_id, level)` 或具有这三个键的字典；`subject_id` 统一转为字符串，`level` 取 `header` 或 `line`。模块不引用业务表，不校验业务单据的读写权；调用方必须先完成这项校验。服务只 flush，提交、回滚和访问审计由调用方负责。

- `define_field(db, *, subject_type, level, name, viewer, owner_scope='personal', value_type='text', choices=None, default_rule_id=None, auto_include=True, sort=0)` 创建定义。个人 owner 使用 `UserContext.user_id`，团队 owner 为 NULL。调用方负责团队权限，定义默认规则必须属于当前填写人。
- `update_field(db, field_def, **changes)` 支持名称、类型、选项、默认规则、默认带出、排序与归档；调用方先校验个人归属或团队权限。已有历史值的定义不允许改类型、选项，以免重新解释历史数据。`archive_field(db, field_def, viewer=None)` 是归档入口；传 viewer 时校验个人归属，团队权限仍由调用方校验。
- `list_defs(db, subject_type, level, viewer)` 合并自己的个人池与团队池，同名团队优先，排序为 `(sort, name, id)`，排除归档定义。
- `template_for(db, subject_type, level, viewer, anchor=None)` 返回默认带出的定义和该查看者、该 anchor 最近 180 天使用过的定义的并集，180 天整包含在内；只返回定义，不读取历史值。
- `save_values(db, subject, values, viewer, anchor=None)` 按提交项增改删，未传项保留。每项必须在 `field_def_id` 和 `new_name` 中选择一项。新名称复用当前可选的同名定义，否则建个人文本定义并默认带出；即使新名称的值为空，也保留池定义。空字符串或 NULL 删除字段值，不写使用计数。非空值最多 2000 字；number 接受有限十进制，date 严格为有效的 `YYYY-MM-DD`，bool 为 `true/false`（也接受 JSON bool），choice 必须命中选项。
- 每个主体每层最多存储 20 个非空字段值，定义池不受该数量限制。保存依次锁主体、同层团队池和本人个人池，并统一按 id 锁定本次规则涉及的全部分组，防止并发保存突破上限或与字段类型变更交错。savepoint 保证校验失败不会留下本次新定义、规则或使用计数。多个调用方必须使用此入口才能共享并发保护。
- 非空值增加 `field_anchor_usage.uses` 并刷新时间；无 anchor 时用空字符串内部计数槽，仅用于名称联想，不用于模板召回。调用方显式传入的 anchor 必须为 1–64 字。
- 每项的 `rule` 可为 `{mode, user_ids, group_ids}`、本人拥有的规则 id 或 NULL；省略保留现有覆盖规则，NULL 清除覆盖并恢复定义默认规则。填写人按最后更新者 `updated_by` 判断，作者本人和调用方 bypass 均可见。
- `read_values(db, subjects, viewer, bypass)` 返回 `{subject_id: [{field_def_id, name, value}]}`，一次只接受一种 subject_type，避免返回键冲突。历史值保留原定义 id，定义改名、归档不删除值；即使定义已归档或被团队同名定义覆盖，仍允许清空当前主体中本人个人池或团队池的已有值，禁止重新写入旧定义。有效规则由值覆盖优先、定义默认其次，交 M8 一次批量过滤；不可见项整个省略。
- `name_suggestions(db, subject_type, q, viewer)` 返回 `[{field_def_id, name, uses}]`，包含当前个人池及团队池，按大小写无关子串筛选、累计使用次数降序排序。接口不带 level，前端根据同层定义过滤建议。

定义重名抛 `FieldPoolError(ValueError, status_code=409)`，其他输入错误为 `ValueError`，个人越权为 `PermissionError`。HTTP 接口将它们转为 409、422、403，所有接口都要求有效实名账号。团队写入只注册 `demo -> action_account_manage`，未知 subject_type 拒绝团队写入。

前端 `CustomFieldsEditor` 接收空模板，不拉历史值；新增名称通过定义接口立即保存到个人池，业务值由外层表单保存。临时隐藏只影响当前编辑实例，输出省略空值和隐藏值。因为保存服务为增量语义，编辑已有单据时，调用方需把原来有值、现在清空或隐藏的 id 显式补为 `value: null`，以删除已有值。切换主体或 anchor 时使用组件 key 重建实例。
