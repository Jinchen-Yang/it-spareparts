# 访问审计动作名列宽修复（sys_access_log.action varchar 32→64）

日期：2026-10-16（上海）。决策: 无（纯缺陷修复，不涉及业务口径；纯后端，
随常规合并交付，不走 version.ts 版本仪式）。

## 背景与范围

`sys_access_log.action` 建表即为 varchar(32)，而全仓 156 个审计动作名中
29 个超 32 字符（最长 43：`maintenance_warehouse_integration_reconcile`），
PostgreSQL 拒绝这些 INSERT；写入是 best-effort（失败仅留 warning 不打断
业务），因此业务请求始终正常，但对应访问/安全审计行静默丢失（生产 DB 日志
显示至少自 2026-10-14 起持续发生）。本次仅扩列宽 + 写入侧兜底，不改任何
审计口径、权限模型与查询逻辑。

## 改动

1. **schema（迁移 `baff4b809f12`，down_revision `a40c9e1d7b52`）**：
   `sys_access_log.action` varchar(32)→varchar(64)（最长名 43 + 余量，覆盖
   动态名 `agent_tool:<注册键>` 最长 35）。PG 侧 varchar 加宽为元数据变更，
   不重写表；按仓库惯例带 `SET LOCAL lock_timeout`。downgrade 不静默截断：
   存在超 32 字符行时报错给出行数，导出/清理后方可降回。
2. 模型同步 `String(64)`（`backend/app/models/system.py`）。
3. 写入兜底（`app/security.py` 两个 helper）：`action[:64]` 挡 agent 自报的
   注册表外工具名（无上界）；`username[:64]` 挡登录失败时请求体里无上限的
   尝试名——截断保留事件，优于丢行。失败 warning 补异常类型（只类型名，
   不带可能含敏感值的消息/SQL 参数）。
4. 守护测试 `tests/test_access_log_action_width.py`：AST 静态扫描全部
   写入点（含 auth `_ev`/`ev` 闭包转发与 `SysAccessLog` 直造；**kwargs
   转发等无法静态解析 action 的调用直接失败），新增超长 action 名在 CI
   即失败；动态写入点须登记且站点数精确匹配。迁移验收
   `tests/test_access_log_action_width_migration.py`（升降级形状 + 降级
   遇超长行拒绝）。

## 各列截断风险结论

- `role(32)`：服务端受控值，最长 `maintenance_manager`=19，无需处理。
- `ip_address(64)`：唯一写路径已有 `[:64]` 截断，XFF 只取第一段，无风险。
- `user_agent(300)`：已有 `[:300]` 截断覆盖全部写路径，无风险。
- `resource`：text 列 + 写入侧 `[:500]` 双保险，无风险。
- `username(64)`：登录失败记请求体尝试名（无上限外部输入）→ 本次加
  `[:64]` 截断（见改动 3）。未给 `LoginRequest.username` 加 max_length：
  422 拒绝会让这类尝试彻底失去审计记录，截断更利于暴力破解排查。

## 验证

- 新增用例全绿（静态契约、动态取值、写入截断、告警仅含异常类型、迁移
  升降级形状与降级守卫）。
- 迁移链：临时 PostgreSQL 15 容器库 `alembic upgrade head` →
  `downgrade a40c9e1d7b52` → `upgrade head` 往返通过；`alembic check`
  零漂移；`alembic heads` 单头。
- 42/43 字符动作名经 `record_access_log` 实际落库复现通过（修复前被拒）。
- 相关存量测试回归通过（audit trail / auth lockout / accounts / auth
  fallback / change password / system settings 与 beta access 迁移族 /
  agent tools，共 56 项）。

## 部署与回滚（runbook，获授权后执行）

1. 前置：main 合并（须 @Jinchen-Yang 人工 APPROVED）、CI 两 job 全绿、
   候选 SHA 构建镜像并归档。
2. 顺序：备份 → 替换 app 镜像 → `alembic upgrade head`
   （a40c9e1d7b52 → baff4b809f12，纯加宽、瞬间完成）→ 起流量 → 按现行
   规则打发布标签、记录精确 SHA 与 schema revision。
3. 回滚：镜像回切即可（64 列对旧代码无感）；如需连数据库一并回退，
   停写后 `alembic downgrade a40c9e1d7b52`——若已有超 32 字符的审计行
   会明确报错并给出行数，导出/清理后再降，绝不静默截断审计数据。
