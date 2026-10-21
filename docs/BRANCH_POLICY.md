# 分支与生产审批规则

管理员：杨金臣（yangjinchen），GitHub 账号 **@Jinchen-Yang**。
本规则来自管理员在 2026-10-10 的明确指令及同日后续更正，覆盖历史冲突指引。
后续更正：test 留给远期实验；近期正式功能直接以 main 为基线准备受审批的合并。

## 分支职责

- `main`：受保护的生产发布线，稳定性优先。仅接收管理员批准的发布或修复。
- `test`：远期实验线，保留原 main 的开发成果，包括尚未获准生产的备件循环功能；不是所有近期功能的必经集成线。
- 近期正式功能的 `feat/*`、`fix/*`、`codex/*`、`claude/*` 等从最新 `main` 开发，PR 以 `main` 为目标。只有明确安排的远期实验才从 `test` 开发并合回 `test`。
- 验收环境按需要运行本次候选构建，不代表必须先合入 `test`。生产只能部署管理员批准的 `main` 精确提交。合并不等于部署，实际已部署 SHA 和镜像另记发布记录。
- 不将整个 `test` 自动提升到生产。发布 PR 必须明确本次范围，排除未批准功能和迁移；必要时从 `main` 创建独立发布分支，挑选已验收提交。
- 本仓库是 IT 备件系统；独立商城 IT-shop 的需求不自动成为本仓库的需求。跨系统边界必须先由管理员确认。

## main 的合并规则与分支保护

1. 开发者准备 PR、精确 head SHA、变更范围、CI 结果、验收证据、迁移影响、备份及回滚方案，联系杨金臣管理员审批。
2. **GitHub `APPROVED` review 不再是 main 的强制要求**：管理员杨金臣 2026-10-21 17:06（北京时间）裁定「关掉 itsp 的审批要求，其它保护保留」。该裁定取代原「必须由 @Jinchen-Yang 对当前候选版本提交 GitHub `APPROVED` review」一条；分支保护已不含必需批准、Code Owner 批准、旧批准失效、最后推送者须不同人批准等设置。
3. 保留的 main 分支保护（以 `GET repos/Jinchen-Yang/it-spareparts/branches/main/protection` 2026-10-21 实际返回为准）：
   - 两项必需 CI：`后端测试（pytest + 迁移链验证）`、`前端类型检查 + 构建`，均须通过；
   - strict：分支必须与 main 保持同步（落后 main 须先合入最新 main 并重新通过 CI）；
   - 对管理员生效（enforce admins），管理员同样受上述限制；
   - 禁止强推 main（allow force pushes 关闭）；同时禁止删除 main，且讨论须全部解决（required conversation resolution）。
4. AI 代理即便持有管理员凭据，也不得使用管理员绕过、临时关闭或修改保护、强推 main，或自行认定已获管理员同意合并。
5. main 合并后的生产部署仍须管理员明确授权，先完成备份和可执行回滚方案，再按精确 SHA 发布并做真实环境验收。

## 本次一次性分线

管理员明确授权将原 main 保留为 test，并以现有生产源码重建 main。
这次分线不是后续绕过审批的先例，也不授权部署或修改生产数据。
原 main 提交：`6dc7e7b1b33faebdd606c295fef3d26a0d31dec9`，完整保留在 test 历史中。
生产源码证据见 `docs/releases/2026-10-10-production-baseline.md`（main）。
