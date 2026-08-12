# Test Strategy

> IT 备件系统的测试分层、策略和规范。

## Test Pyramid

```
         ┌──────┐
         │ E2E  │  ← Playwright (前端关键流程)
         ├──────┤
         │ Int  │  ← pytest + httpx (API 集成测试)
         ├──────┤
         │ Unit │  ← pytest (Service/Model 单元测试)
         └──────┘
```

## Test Infrastructure

| 层面 | 工具 | 位置 | 数据库 |
|---|---|---|---|
| 后端单元 | pytest | `backend/tests/test_*.py` (175 文件) | 隔离 PostgreSQL (conftest 每进程建库) |
| 后端集成 | pytest + httpx | 同上 | 隔离 PostgreSQL |
| 前端单元 | vitest + Testing Library | `frontend/src/**/*.test.tsx` (61 文件) | Mock (MSW) |
| E2E | Playwright | `.playwright-cli/` (服务器) | Docker 完整栈 |

**测试数据库：** `postgresql+psycopg://spareparts:spareparts@127.0.0.1:5433/spareparts_test`

## Testing Rules

### 每个新功能必须

1. **后端：** 单元测试覆盖核心逻辑 + API 集成测试覆盖端点
2. **前端：** 组件渲染测试 + 用户交互测试
3. **Beta 功能：** 必须测试"总闸关闭时返回 403/404"

### Bug 修复必须

1. 先写**复现测试**（验证 bug 存在）
2. 再写修复代码
3. 确认复现测试通过
4. 保留复现测试防止回归

### 禁止事项

- ❌ 跳过测试提交代码
- ❌ 注释掉失败测试而非修复
- ❌ 测试依赖外部服务（LLM API、真实文件系统）除非有 mock/fallback

## Current Test Coverage

**后端（`codex/maintenance-manager-combined`）：**
- 总测试文件：175
- 收集到测试：2759
- 通过：2754
- 跳过：5（平台相关或条件跳过）
- 维保测试：62 文件
- Agent 测试：5 文件
- Release 测试：7 文件

**前端（`codex/maintenance-manager-combined`）：**
- 测试文件：61
- 测试通过：789
- 构建：tsc + vite build 通过

## Key Test Files（按模块）

| 模块 | 测试文件 | 测试数 |
|---|---|---|
| AI Agent | test_agent_*.py (5 文件) | runtime, tools, prompts, file ACL, hardening |
| 维保项目 | test_maintenance_project*.py | 项目 CRUD, 合同, 操作 |
| 维保工作簿 | test_maintenance_workbook*.py | v2, v3, 导出, 渲染 |
| 维保成本 | test_maintenance_cost*.py | 双税, 取价链, 质量 |
| 维保迁移 | test_maintenance_migration*.py | 控制, 旧数据, runs |
| 坏件返还 | test_maintenance_bad_returns*.py | API, 逻辑, 迁移 |
| 补库 Beta | test_replenishment_beta*.py | 权限, 购物车 |
| 数据质量 | test_data_quality*.py | 校准, 异常检测 |
| 发布控制 | test_v120_release_control.py | shellcheck, manifest, 环境 |
| Boss 看板 | test_boss_v2_mask*.py | 数据掩码, KPI |

## CI Pipeline (GitHub Actions `ci.yml`)

```yaml
1. PostgreSQL 15 容器启动（spareparts_test, port 5433）
2. uv sync --extra dev
3. alembic upgrade head
4. alembic check（零漂移检查）
5. uv run --extra dev pytest -q（后端全量）
6. 后端依赖漏洞扫描
7. npm install
8. tsc && vite build（前端构建）
9. npm run test（前端全量）
```
