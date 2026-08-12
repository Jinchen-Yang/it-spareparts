# API Design

> REST API 设计规范和当前端点清单。

## API Conventions

### URL 结构

```
/api/{resource}              → 集合操作
/api/{resource}/{id}         → 单个资源
/api/{resource}/{id}/{action} → 子资源或动作
```

### HTTP 方法

| 方法 | 用途 | 示例 |
|---|---|---|
| GET | 查询 | `GET /api/parts?keyword=xxx` |
| POST | 创建 | `POST /api/imports/upload` |
| PUT | 全量更新 | `PUT /api/parts/{id}` |
| PATCH | 部分更新 | `PATCH /api/accounts/{id}/permissions` |
| DELETE | 删除（逻辑） | `DELETE /api/maintenance/demands/{id}` |

### 响应格式

**成功：**
```json
{
  "data": { ... },
  "total": 100,
  "page": 1,
  "page_size": 20
}
```

**错误：**
```json
{
  "detail": "人类可读的错误描述"
}
```

### 鉴权

所有端点（除 `/api/auth/login`）必须：
- Header: `Authorization: Bearer <token>`
- RBAC 检查：`Depends(get_current_user_context)` + `Depends(require_page("page_xxx"))`

## Current API Endpoints（34 个 Router）

### 认证与账号
| Method | Path | Description | Auth |
|---|---|---|---|
| POST | `/api/auth/login` | 登录获取 token | None |
| GET | `/api/accounts` | 账号列表 | Admin |
| POST | `/api/accounts` | 创建账号 | Admin |
| PUT | `/api/accounts/{id}` | 更新账号 | Admin |
| GET | `/api/role-templates` | 角色模板 | Admin |

### 备件管理
| Method | Path | Description |
|---|---|---|
| GET | `/api/parts` | 型号搜索/列表 |
| GET | `/api/parts/{id}` | 型号详情 |
| POST | `/api/parts` | 创建型号 |
| PUT | `/api/parts/{id}` | 更新型号 |
| GET | `/api/substitutes` | 替代组管理 |
| GET | `/api/inventory` | 库存列表 |
| POST | `/api/imports/upload` | 上传导入文件 |
| POST | `/api/imports/precheck` | 导入预检 |

### 采购与利润
| Method | Path | Description |
|---|---|---|
| GET | `/api/purchases` | 采购记录 |
| GET | `/api/profit` | 利润分析 |
| GET | `/api/pools` | 互通池列表 |
| GET | `/api/pool-analysis/{groupId}` | 池分析 |

### 看板与治理
| Method | Path | Description |
|---|---|---|
| GET | `/api/dashboard/*` | 看板 KPI |
| GET | `/api/governance/*` | 数据治理 |
| GET | `/api/data-quality/*` | 数据质量 |

### 维保管理（12 个 Router，均需 Beta 白名单）
| Method | Path | Description |
|---|---|---|
| GET | `/api/maintenance/projects` | 项目列表 |
| POST | `/api/maintenance/projects` | 创建项目 |
| GET | `/api/maintenance/projects/{id}/workspace` | 项目工作台（四表） |
| GET | `/api/maintenance/projects/{id}/scope` | 项目范围 |
| POST | `/api/maintenance/projects/{id}/contracts` | 添加合同 |
| GET | `/api/maintenance/manager-workbooks` | 月报工作簿 v3 |
| POST | `/api/maintenance/manager-workbooks/generate` | 生成工作簿 |
| POST | `/api/maintenance/acceptance` | 提交验收 |
| GET | `/api/maintenance/warehouse` | 现场领用 |
| POST | `/api/maintenance/demands/delete` | WBDD 安全删除 |
| GET | `/api/maintenance/bad-returns` | 坏件返还列表 |
| POST | `/api/maintenance/source-assignments` | 来源单归属 |
| GET | `/api/maintenance/migration` | 成本迁移状态 |
| POST | `/api/maintenance/migration/execute` | 执行迁移 |

### 补库 Beta
| Method | Path | Description |
|---|---|---|
| GET | `/api/replenishment/search` | PN 搜索 |
| POST | `/api/replenishment/cart` | 加入购物车 |
| POST | `/api/replenishment/submit` | 提交审核 |

### AI 助手
| Method | Path | Description |
|---|---|---|
| POST | `/api/agent/chat` | AI 对话（支持 tool calling） |
| POST | `/api/agent/chat/stream` | AI 流式对话 (SSE) |
| GET | `/api/chat-sessions` | 对话历史 |

### 系统设置
| Method | Path | Description |
|---|---|---|
| GET | `/api/system-settings` | 获取设置（含 Beta 开关） |
| PUT | `/api/system-settings` | 更新设置 |

## API Design Rules

1. **向后兼容：** 修改已有接口参数/响应结构必须讨论，优先新增字段而非修改
2. **分页：** 列表接口默认 page=1, page_size=20，返回 total
3. **搜索：** 使用 query param `keyword`，模糊匹配
4. **文件上传：** multipart/form-data，大小限制 10MB
5. **Beta 端点：** 路径不变，内部通过总闸守卫控制可达性
6. **错误码：** 400 参数错 / 401 未登录 / 403 无权限 / 404 不存在或未启用 / 422 校验失败 / 500 内部错
