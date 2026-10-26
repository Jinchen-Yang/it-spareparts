# 字段池与受众组件接入契约

- `CustomFieldsEditor` 只接收通用的 `subjectType`、`level` 和字段定义 `template`，不读取业务表或路由。模板由调用方通过 `getFieldTemplate` 获取，调用方可以透传 `anchor` 来带出上次使用过的空框。组件不会把历史值带入新建单据。
- 一张单据或一行使用一个组件实例。切换单据、行或 anchor 时，以对应标识作为 React `key`，使临时隐藏、草稿值和可见性覆盖随实例重置。编辑已有数据时，在 `value` 中传字段值，并在 `template` 或 `definitions` 中提供对应定义。
- 新增已有字段只添加空框。新建名称会立即调用 `POST /fields/defs`，创建 `personal`、`text`、`auto_include=true` 的定义，因此即使本单留空，字段池仍可在下次带出它。
- `onChange` 返回有值字段的完整列表，空字符串被省略；`"0"` 和 `"false"` 保留。临时隐藏的字段也从列表中移除，不改模板、不归档字段。编辑已有业务记录时，业务保存适配层需对照原值，把清空或移除的字段转换成后端 `save_values` 所需的显式空值删除项。
- 定义的 `default_rule` 决定初始锁图标；字段自己的 `rule` 优先。未设置覆盖时不输出 `rule`，由后端应用定义默认规则。前端只负责编辑规则，最终可见性始终由后端 M8 判断。
- `AudiencePicker` 和 `AudienceLock` 支持受控 `value` / `onChange`，以及可选的 `people`、`groups` 候选；未传候选时使用通用接口。公开和私密不带成员，两个选择面板互斥，清空最后一个成员返回公开。
- Picker 当场创建的分组为个人分组；公共分组管理由调用方单独提供。归档分组不列入新规则候选。
- 常用分组按浏览器本地保存的选择次数排序，不改变分组数据或权限。调用方可通过 `audienceProps.preferenceKey` 按账号隔离偏好；不传时使用浏览器默认偏好键 `audience.group-frequency`。
