# M8 受众可见性契约

M8 只保存通用受众规则和分组。调用方先校验记录访问权，再传入 `UserContext`、内容作者和 `bypass(viewer)`；模块不读取任何业务权限键，也不替代调用方已有的字段权限。

账号身份使用 `UserContext.user_id`，即实名账号的 `SysUser.username` 字符串；规则、分组内部主键为整数。新建规则或维护分组时，显式选择的人必须是在职、未软删除账号。个人分组仅 owner 可维护、发现和引用；公共分组所有人可引用，只有 `bypass` 为真的人可以维护。个人分组即便主管也不能代改。

- `create_rule(db, mode, user_ids, group_ids, owner) -> int`：`owner` 可为 `UserContext` 或账号字符串；公开/私密不可带成员，部分可见/不给谁看至少一个成员。成员去重；归档组禁止新引用。
- `can_see(db, rule_id, viewer, *, author_id, bypass) -> bool`：作者和 bypass 恒可见；空规则等同公开；私密仅前述两者可见。include 命中人员或任一分组可见，exclude 命中则隐藏。
- `filter_fields(db, records, viewer, *, field_rules, bypass) -> list[dict]`：`field_rules` 为字段键到 `(rule_id, author_id)` 的映射，支持整数等可哈希键。返回字典浅拷贝，不可见字段整个删除，未配置规则的键保留。多记录和多规则共用一次联表 SELECT；作者或 bypass 无需查规则。
- `list_groups(db, viewer)`、`create_group(db, name, scope, user_ids, owner, *, bypass)`、`update_group(db, group_id, viewer, *, name=None, user_ids=None, bypass)`、`archive_group(db, group_id, viewer, *, bypass)` 返回 ORM 分组。成员列表在 `AudienceGroupMember` 中；省略 `user_ids` 不修改，空列表清空。

分组按当前成员读取，更新后立即影响所有历史规则。归档仅关闭发现、新引用和后续编辑，保留当前成员及已有规则的可见性。并发创建规则与归档分组使用相同的分组行锁。规则不提供原地修改，调用方通过新规则替换引用。

未知规则、残缺的 include/exclude、非法规则模式均保守拒绝；作者和 bypass 例外仍优先。异常为 `AudienceError(ValueError)`，带 `status_code` 和 `code`：非法输入 422、无权 403、不存在/已归档 404、重名 409。服务仅 flush，不 commit，事务和访问审计由调用方负责。

数据库迁移同时登记 M7 所需结构。团队字段 owner 为空，活动名称唯一索引使用 `NULLS NOT DISTINCT`，归档后可重新定义同名字段；`field_anchor_usage.uses` 初值为 1。规则成员附加整数主键，二选一检查约束保证人员/分组恰有一个，部分唯一索引阻止同规则重复成员。
