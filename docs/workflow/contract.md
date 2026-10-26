# M5 待办 / M6 时间线契约

业务层注册 `SubjectSpec`，向引擎传入 `Subject(subject_type, subject_id)`；引擎不导入业务表。
生产默认没有注册任何业务。测试仅注册 `demo`，业务使用方在启动时自行注册。

## 注册与身份

| 接口 | 契约 |
|---|---|
| `register(subject_type, spec)` | 同类型不可重复注册；未注册类型抛 `NotFound`（HTTP 404） |
| `pool_permission` | 池成员与指派目标所需权限，按账号现有权限快照和覆盖计算 |
| `supervisor_permission` | `all` 页签和主管改派的权限；只对相应 subject_type 生效 |
| `can_view_subject(db, subject_id, ctx)` | 创建待办及读取时间线的业务对象准入回调 |
| `summarize(db, subject_ids, ctx)` | 每种类型一次批量摘要；结果按主体 ID 索引 |
| `filter_events(events, ctx)` | 时间线字段/受众过滤出口；默认原样返回，过滤后再分页 |
| `pool_key` | 可选，默认等于 subject_type；相同池必须配置相同成员权限 |

实名账号来自 `sys_user`。HTTP 通过 `current_identity` 校验 token，拒绝共享口令身份；
服务层每次重新解析有效账号与权限。服务动作的 `actor` 接受用户整数 ID 或已有的
`UserContext`（其中 user_id 是用户名）。用户外键类型均为 Integer。

## 事务与动作

所有服务函数只 flush、不 commit；状态和 `subject_event` 在同一事务提交或回滚。
HTTP 写接口负责提交，成功后调用 `record_access_log`。
`Forbidden / Conflict / InvalidTransition / NotFound` 分别映射为 403 / 409 / 422 / 404。

- `open_items`：非空 assignee_ids 去重后每人一条 pending；空列表产生一条公共池待办。
  指派目标必须是在职池成员，pool_key 必须与注册表匹配。
- `claim`：条件 UPDATE 同时检查空 assignee、pending、版本和未暂停；成功写入领取人并进入
  in_progress。竞争失败返回当前领取人 ID、显示名与版本。
- `start`：已指派 pending → in_progress；`complete / decline` 结束已指派的待办。
- `release`：仅领取人可以退回公共池；主管收回使用 reassign。领取标记与领取时间一并清空。
- `transfer`：当前处理人或主管可转交给另一个在职池成员，回到 pending。
- `reassign`：仅主管可用，目标为 None 时收回公共池；否则目标必须是在职池成员。
- release / transfer / reassign / decline / cancel 必须提供非空原因。
- 每个单条修改必须带 `expected_version`；成功一次递增一次。结束状态不能再次转换。
- 暂停期间禁止 claim、start、complete、decline；仍可退回、转交、主管改派或取消。
- `cancel_subject / pause_subject / resume_subject`：仅创建这些待办的发起人或主管可调用，
  以固定 ID 顺序锁定全部未结束待办。必须传 `expected_versions: {item_id: version}`，
  对全部未结束待办检查调用方版本；缺失版本拒绝执行。已暂停/已恢复的重复调用无新增事件。

数据库对同一 subject + assignee 的 pending/in_progress 强制唯一（NULL 公共池待办不互斥）。
唯一约束冲突返回 Conflict；调用方必须回滚失败事务。
时间线 UPDATE / DELETE 被数据库触发器拒绝，系统事件允许 actor=None。

## 列表与接口

所有路径挂载在现有 `/api` 前缀下。

| 路径 | 响应 |
|---|---|
| `GET /work-items?tab=&subject_type=&page=&page_size=` | `{items,total,page,page_size,can_supervise}` |
| `GET /work-items/badge` | `{assigned,pool,claimed}`；三个计数使用一次 SQL 聚合 |
| `GET /work-items/candidates?pool_key=` | `{items:[{id,display_name}]}`；实名登录即可枚举注册池在职成员 |
| `POST /work-items/{id}/{action}` | 修改后的 item；body 为 expected_version 及所需 reason/to_user_id |
| `GET /subjects/{subject_type}/{subject_id}/events?page=&page_size=` | `{items,total,page,page_size}`；过滤后按 created_at、id 倒序 |

分页从 1 开始，page_size 默认 20、最大 100。每条 item 带 summary、assignee_name、
claimed_by_name、allowed_actions。事件附 actor_name，系统动作 actor_name 为空。

- assigned：直接指派给我且未结束；不包含领取记录。
- pool：我具有成员权限且未指派的 pending 记录。
- claimed：我领取且未结束。
- done：我实际执行过处理动作（claim/start/release/transfer/complete/decline）且已结束，
  closed_at 在最近 90 天内。仅创建或主管改派不算处理。
- all：我拥有对应主管权限的全部待办，包含已结束记录；其他类型不会因一处主管权限而放行。

同级成员看不到指派给别人的待办。发起人查看整体对象进度由业务对象接口及时间线回调提供，
不会因此得到全池列表权限。池摘要的业务字段和受众由 summarize 回调控制。

前端只提供 WorkInbox、useWorkBadge、Timeline，不新增页面路由。
WorkInbox 使用服务端 allowed_actions；动作完成广播角标刷新，冲突刷新列表。
角标每 60 秒轮询，页面隐藏时暂停，恢复可见时立即刷新。
