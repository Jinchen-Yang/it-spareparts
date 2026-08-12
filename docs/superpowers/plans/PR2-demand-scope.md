# PR 2：维保需求单按项目负责人范围隔离

> 基线：`origin/main@caf4a973` | 依赖：PR 1（路由稳定后）| Issue：`[MAINT-SEC-P0]`

## 1. 要解决的问题 (Problem)

当前维保需求单（WBDD）搜索和删除 API 只验证 `page_maintenance_beta` 页面权限，不验证操作者是否属于该需求单所关联项目的负责人。这意味着：

- 维保负责人 A 可以通过修改请求参数看到负责人 B 项目下的需求单；
- 未分配项目的需求单对所有有 Beta 权限的账号可见；
- API 层 `require_page("page_maintenance_beta")` 是布尔门——有权限就全看，没权限就全看不见，中间没有"只看自己项目"的粒度。

## 2. 达成的目的 (Goal)

- admin 看到全部维保需求单（包括未分配项目的），以及全部删除/恢复操作
- 维保负责人只能搜索、查看和操作本人负责项目的需求单
- 无项目归属的维保负责人（有权限但没分配到任何项目）看到空列表
- 指定无权访问的 `project_id` 参数时返回 403，而非空列表
- 安全删除和恢复继续服从原有权限与引用保护

**本次不改：**
- 不修改前端页面结构（PR 3 做）
- 不新建"项目经理"角色——复用现有 `project_manager_id` 字段
- 不改变 `page_maintenance_beta` 权限键（等 PR 1 一起清理）

## 3. 实现的路径 (Implementation Plan)

### Step 1: 新建项目范围工具模块

- [ ] 新建 `backend/app/api/maintenance_project_scope.py`
  - `resolve_project_ids_for_user(db, user_context) → set[str]`：admin 返回哨兵值表示"全部"；普通用户从 `maintenance_project` 表查 `project_manager_id == user.sub` 的项目列表
  - `require_project_access(project_ids, target_project_id) → None | 403`：验证越权时直接抛 HTTPException

### Step 2: 写失败测试

- [ ] 新建 `backend/tests/test_maintenance_demand_project_scope.py`
  - `test_admin_sees_all_demands`：admin 搜索返回所有需求单
  - `test_manager_sees_only_own_project`：维保负责人 A 看不到负责人 B 项目的需求单
  - `test_manager_without_project_sees_nothing`：无项目归属的维保负责人搜索返回空列表
  - `test_unassigned_demand_visible_to_admin_only`：未分配项目的需求单对普通账号不可见
  - `test_unauthorized_project_id_returns_403`：指定无权 `project_id` 时不返回空列表而是 403
  - `test_safe_delete_scoped`：删除操作也服从项目范围隔离
  - `test_restore_scoped`：恢复操作也服从项目范围隔离

### Step 3: 改后端

- [ ] `backend/app/services/maintenance_demands.py`
  - `search_demands()` 新增参数 `allowed_project_ids: set[str] | None`（None = admin 全量）
  - 过滤条件：如果 `allowed_project_ids` 不是 None，WHERE 子句加 `project_id IN allowed_project_ids`
  - 未分配项目的需求单只在 `allowed_project_ids is None`（admin）时返回

- [ ] `backend/app/api/maintenance_demands.py`
  - 所有 endpoint 注入 `Depends(get_current_user_context)`
  - 在调用 service 前，通过 `maintenance_project_scope.resolve_project_ids_for_user()` 获取范围
  - `project_id` query param 存在时，调 `require_project_access()` 验证

- [ ] 审计字段：在 `SysAccessLog` 中记录查询范围标记（admin 全量 vs 项目范围），不记录具体项目 ID 列表

### Step 4: 验证

```bash
cd backend
uv run --extra dev pytest -q \
  tests/test_maintenance_demand_project_scope.py \
  tests/test_maintenance_demand_safe_delete.py
```

## 4. 验收标准 (Acceptance Criteria)

- [ ] admin 搜索 → 全部需求单（含未分配项目的）
- [ ] 维保负责人 A 搜索 → 仅 A 负责项目的需求单
- [ ] 无项目维保负责人搜索 → 空列表
- [ ] `project_id=无权项目ID` → 403，不是 200 空列表
- [ ] 安全删除 / 恢复遵守同等范围
- [ ] 前端无法通过修改请求参数绕过

## 5. 影响面与风险 (Impact & Risk)

- **改动文件数**：5 个（1 新建 scope 模块 + 2 改现有 + 2 测试文件）
- **是否架构变动**：否（新增工具模块，不改架构分层）
- **是否破坏已有接口**：是——现有 Beta 账号如果没分配项目，搜索会从"有结果"变成"空列表"。需要在 PR 描述中说明这是**行为修正**，不是 regression
- **是否需要数据迁移**：否
- **已知风险**：`project_manager_id` 当前存的是字符串标识而非外键到 `sys_user`，需要用 `user_context.sub`（username）匹配。如果负责人标识和登录名不一致，匹配会失败 → 需要和 PR 0 口径一致
