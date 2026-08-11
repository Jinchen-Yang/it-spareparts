# Development Rules

> 所有 AI Agent 和人类开发者必须遵守。违反者 Code Review 打回。

## Coding Principles

### 1. Keep It Simple

- 禁止引入不必要的抽象层（"未来可能需要"不是理由）
- 优先使用标准库和已有依赖，避免新增第三方包
- 一个函数不超过 80 行（工具函数/迁移脚本除外）
- DRY 是手段不是目的——三个重复再抽象

### 2. Single Responsibility

每个模块/类/函数只做一件事：
- Router：路由 + 参数校验 → 调 Service
- Service：业务逻辑 → 调 Model/Repository
- Model：数据定义 + 基础查询
- 禁止 Router 里写 SQL
- 禁止 Service 直接操作 HTTP 请求/响应对象

### 3. No Duplicated Logic

- 相同的业务规则必须有单一来源（Single Source of Truth）
- 价格计算、成本取价、权限判断等核心逻辑集中在一个模块
- 发现重复立即抽取，别等"下次重构"

### 4. Type Safety

**Backend (Python)：**
- 所有函数签名必须有 type hints
- 所有 API 请求/响应必须有 Pydantic model
- 禁止 `dict`/`Any` 作为公开接口类型（内部工具函数除外）

**Frontend (TypeScript)：**
- `strict: true`（tsconfig 已配）
- 禁止 `any`，特殊场景用 `unknown` + type guard
- API 响应必须有 interface 定义

### 5. Error Handling

**Backend：**
- 所有异常必须明确类型：`raise HTTPException(status_code=..., detail=...)` 或自定义异常
- 关键操作记录日志（`_log.info`/`_log.error`）
- 禁止裸 `except:` 吞异常（至少 `except Exception:` 并记录）
- 外部 API 调用必须有超时和重试策略

**Frontend：**
- API 调用必须有 error boundary 或 try/catch
- 网络错误显示用户友好提示，不暴露技术细节
- 表单提交必须有 loading + error 状态

### 6. Security

**绝对禁止：**
- ❌ 硬编码密钥、密码、token（全部走 `.env` 或环境变量）
- ❌ SQL 字符串拼接（用 SQLAlchemy ORM 参数化查询）
- ❌ 用户输入直接拼入 HTML/JS（React 默认转义，但 `dangerouslySetInnerHTML` 必须评审）
- ❌ 敏感数据写入日志（密码、token、身份证号、手机号）
- ❌ 生产凭据提交到 Git

**必须执行：**
- ✅ 所有 API 端点鉴权（`Depends(get_current_user_context)`）
- ✅ 敏感操作二次确认（删除、Beta 放行、迁移执行）
- ✅ 文件上传校验（大小、类型、ACL）
- ✅ RBAC 权限检查（`require_page` + `current_role`）

### 7. Testing

- 每个新功能必须有单元测试
- Bug 修复必须先写复现测试
- 维保业务逻辑测试不依赖真实数据库（conftest 已提供隔离 PostgreSQL）
- 测试覆盖率不作为硬指标，但关键路径（成本计算、权限、数据导入）必须覆盖

### 8. Database

- 所有 schema 变更必须有 Alembic 迁移脚本
- 迁移脚本必须可逆（`downgrade()`）
- 禁止手动修改生产数据库
- 合并前跑 `alembic check` 确保无漂移
- 禁止从同一 revision 分叉创建多个 head（保持线性历史）

### 9. Git

- 分支命名：`{type}/{scope}` — 如 `codex/issue-xxx`, `fix/xxx`, `feat/xxx`
- Commit message：中文描述 + 英文 prefix（`fix:`, `feat:`, `test:`, `docs:`, `chore:`）
- PR 前 rebase 到最新 main
- Squash-merge 到 main
- 禁止 force push main 和共享分支

### 10. Documentation

- API 变更必须更新 `.ai/API_DESIGN.md`
- 架构变更必须写 ADR（`.ai/DECISIONS.md`）
- 新模块必须更新 `.ai/ARCHITECTURE.md`
- 任务完成后更新 `.ai/CHANGELOG.md`
