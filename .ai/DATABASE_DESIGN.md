# Database Design

> PostgreSQL 15 数据库设计规范和关键表结构。

## Database Info

| 项目 | 值 |
|---|---|
| 引擎 | PostgreSQL 15 |
| 开发库 | `spareparts` (Docker `:5432`) |
| 测试库 | `spareparts_test` (Docker `:5433`) |
| ORM | SQLAlchemy 2.0 |
| 迁移 | Alembic（62 个迁移脚本，单 head 链） |
| 当前 Head | `d9f1a3c7e5b2` |

## Key Tables by Domain

### 备件主数据
| 表 | Model | 说明 |
|---|---|---|
| `parts` | master_data.py | 备件型号（品牌、规格、PN、价格） |
| `substitute_groups` | master_data.py | 替代组 |
| `part_pool` | (models) | 互通池 |
| `inventory` | inventory.py | 库存记录 |
| `purchase_records` | purchase.py | 采购记录 |
| `sales_records` | sales.py | 销售记录 |

### 维保核心
| 表 | Model | 说明 |
|---|---|---|
| `maintenance_projects` | maintenance_project.py | 维保项目主档 |
| `maintenance_contracts` | maintenance_project.py | 维保合同 |
| `maintenance_project_operations` | maintenance_project_operations.py | 项目操作事实（append-only） |
| `maintenance_consumptions` | maintenance_warehouse.py | 现场领用记录 |
| `maintenance_bad_returns` | maintenance_bad_return.py | 坏件返还义务 |
| `maintenance_demands` | maintenance.py | 维保需求单（WBDD） |
| `maintenance_source_assignments` | maintenance_source_assignment.py | 来源单归属 |
| `maintenance_manager_workbook` | maintenance_manager.py | 月报工作簿 |
| `maintenance_migration_*` | maintenance_migration.py | 成本迁移控制 |
| `maintenance_cost_reference` | maintenance_project_operations.py | 成本取价链冻结证据 |

### 补库 Beta
| 表 | Model | 说明 |
|---|---|---|
| `replenishment_carts` | replenishment.py | 补库购物车 |
| `replenishment_reviews` | replenishment.py | 补库审核记录 |

### AI / Agent
| 表 | Model | 说明 |
|---|---|---|
| `chat_sessions` | chat.py | Chat 对话会话 |
| `chat_messages` | chat.py | Chat 消息历史 |

### 权限与系统
| 表 | Model | 说明 |
|---|---|---|
| `accounts` | (auth) | 用户账号 |
| `role_templates` | (auth) | RBAC 角色模板 |
| `system_settings` | system.py | 系统设置（单例，含 Beta 开关） |
| `maintenance_beta_allowlist` | (beta) | Beta 白名单 |
| `business_settings` | system.py | 业务参数 |
| `data_quality_issues` | data_quality.py | 数据质量异常 |
| `import_jobs` | (etl) | 导入任务记录 |
| `audit_log` | (security) | 访问审计日志 |

## Migration Rules

1. **所有 schema 变更必须有 Alembic 迁移脚本**
2. **迁移必须可逆**（`upgrade()` 和 `downgrade()` 都要实现）
3. **单 head 策略：** 合并多个分支时用 merge migration 保持线性
4. **迁移前验证：** CI 中 `alembic check` 检查无漂移
5. **数据迁移分离：** schema 变更和数据变更分开执行
6. **禁止在迁移中执行不可逆操作：** DROP TABLE、DROP COLUMN 需要额外的安全确认

## Migration Chain (简化的线性历史)

```
fb60839c775d  ← initial schema v1.1
     │
     ├── ... 基础功能迁移 ...
     │
     ├── c6f2a8e9d4b1  ← maintenance project contract foundation
     ├── d8a3c7e4f2b1  ← controlled maintenance project master
     ├── e2f4a6c8b1d3  ← maintenance project operating facts
     ├── f3b7d9e1c5a2  ← workbook validation retention index
     ├── a4c9e1f2b6d8  ← first-class maintenance fact provenance
     ├── b7d2f4a6c8e1  ← stable project dual-tax costs
     ├── c4e8a1d7f2b6  ← strict operating status pairs
     │     │
     │     ├── e6f1a9c3b7d2  ← source order assignment
     │     ├── f4b8c2d1e7a6  ← WBDD safe delete
     │     ├── f4b8d2e6a1c3  ← site issue v2
     │     ├── e6a9c3f1b2d4  ← operation audit append-only
     │     │     │
     │     │     ├── a6c8d2e4f1b7  ← manager assignments → b7e1 → c8 (workbook v3)
     │     │     ├── a8d3 → b6 (bad returns)
     │     │     └── c2f7 (migration controls)
     │     │
     │     ├── d3e5f7a9c1b2  ← merge 204/205/207
     │     ├── e4f6a8c2d1b3  ← merge 208
     │     └── f5a7c9e1b3d4  ← merge 206
     │
     ├── a6d1e9c3b7f2  ← warehouse adapters
     ├── b2c4e6f8a1d3  ← merge assignment/warehouse + shipment bridge
     ├── d3e5f7a9b1c2  ← merge bridge + cutover
     ├── c7e2a9f4b6d1  ← replenishment cart Beta
     └── d9f1a3c7e5b2  ← maintenance Beta whitelist (CURRENT HEAD)
```

## Performance Notes

- 型号搜索：使用 ILIKE + trigram 索引（已建）
- 导入预检：批量 INSERT ON CONFLICT UPDATE
- 大表查询：分页 + 索引覆盖（`order_status_date_indexes`）
- Token 版本号：`token_version` 列用于强制旧 token 失效
- 维保操作事实表：append-only，按 `created_at` 分区准备中

## Security Notes

- 数据库端口只绑 `127.0.0.1`（Docker 和本地均不暴露外网）
- 密码通过环境变量注入（`POSTGRES_PASSWORD`），不写死在代码中
- 生产密码 ≥ 16 位强随机
- CI 测试库 `spareparts_test` 每次 pytest 进程独立建库/拆库
