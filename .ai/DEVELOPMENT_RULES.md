# Development Rules

> 所有 AI Agent 和人类开发者必须遵守。违反者 Code Review 打回。

## 0. 开发留痕协议（硬性要求，所有开发必须遵守）

> 目标：**每次开发都留痕、可回溯，任何时候都能讲清"改动前是什么、改了什么、为什么改、架构怎么变的"**。
> 判定标准：不满足以下任何一条，视为未完成，Code Review 打回。

### 0.1 留痕（每次开发必须产出 4 类痕迹）

1. **任务痕迹**：开发前在 `.ai/CURRENT_TASK.md` 登记任务（目标 + 影响范围 + 完成标准）；完成后勾选并注明结果
2. **变更痕迹**：开发完成后在 `.ai/CHANGELOG.md` 追加记录，**必须包含**：
   - 日期 + Agent/开发者 + 会话/任务名
   - **改动前状态（before）**：被改模块/行为"原本是什么"（一句话讲清现状）
   - **改动内容（after）**：改了什么、涉及哪些文件（路径级）
   - **改动原因**：对应 Issue/PR 编号或需求来源
   - 验证结果（测试数、lint、构建）+ **commit SHA**（可回溯锚点）
3. **提交痕迹**：commit message 必须能独立回答"改了什么、为什么"，格式 `type(scope): 中文描述 (#issue)`；一个 commit 只做一件事
4. **架构痕迹**：任何架构级变动（分层、模块、数据流、依赖、迁移、协议）必须写 ADR 到 `.ai/DECISIONS.md`（含"原有设计→新设计→原因→影响"），并同步更新 `.ai/ARCHITECTURE.md`

### 0.2 可回溯（任意时间点可还原"当时为什么这么做"）

- 从任一 commit SHA 出发，必须能在 `.ai/CHANGELOG.md` 找到对应记录（CHANGELOG 记录 commit SHA 与 before/after）
- 从任一架构现状出发，必须能在 `.ai/DECISIONS.md` 找到其来源 ADR（未写 ADR 的架构 = 未完成）
- 从任一 API/表结构出发，必须能在 `.ai/API_DESIGN.md` / `.ai/DATABASE_DESIGN.md` 找到当前契约（契约与代码不一致 = 未完成）
- Agent 接替时按 `.ai/AI_WORKFLOW.md` Handoff 协议执行，**代码和文档是唯一真相，不依赖聊天历史**

### 0.3 讲得清"原有 vs 架构变动"（每个开发周期的收尾检查）

开发结束时必须能回答以下问题（写不进文档的变更视为未完成）：

1. 改动前这个模块/行为的**原有状态**是什么？（示例："字段 X 原为自由文本，导入时直接落库"）
2. 改动后**变成了什么**？（示例："X 改为枚举校验 + 规范化存储，非法值拒绝导入"）
3. **为什么改**？（对应 Issue/业务原因，不允许"顺手改"）
4. 是否涉及**架构变动**？若有 → ADR 写了没有？ARCHITECTURE.md 同步了没有？
5. 影响面：改了哪些文件、哪些模块被调用方依赖、测试覆盖了哪些路径？

### 0.4 禁止项

- ❌ 无 CHANGELOG 记录的代码变更（临时调试文件除外，但必须清理）
- ❌ 架构变动不写 ADR 直接提交
- ❌ commit message 无法独立说明"改了什么、为什么"（如 "update"、"fix stuff"）
- ❌ 文档与代码不一致却继续合入
- ❌ 用"我上次说过/我记得"代替文档记录

---

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
