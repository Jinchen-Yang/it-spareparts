# Current Task

> 最后更新：2026-08-11

## Goal

完成 IT_data 项目从旧服务器（cloudlay-ubuntu）到新开发环境（WSL Ubuntu 26.04）的完整迁移，恢复全部开发能力。

## Status: 开发环境迁移

### TODO

- [x] 推送 3 个服务器独有 codex 分支到 GitHub（replenishment-cart, issue201-formal-review, fix-maintenance-return-rate-lock）— 2026-08-11 完成
- [x] 修复 #228 表格清洗的两个 P1 安全漏洞 — 2026-08-11 完成：ProposedValueSnapshot 绑定 + Assessment 容器镜像上限；commit `be56fc87`，PR #242 draft，33 focused tests 绿
- [x] 找到/重建 #227 补库审核新 SHA `8fd395a` — 2026-08-11 从旧服务器 `/tmp/it-spareparts-artifact.m1cbgs` 找回，已验证 68 tests + Ruff(0.14.6) + format + py_compile + diff check，已推送 origin，PR #239 head = 8fd395a
- [ ] 审查 #235 Capability Kernel（人工审查）
- [ ] 在 Capability 之上线性重建 Artifact Delivery (#236)
- [ ] 从 Capability→Artifact head 选择性重放 Durable Task (#234)
- [ ] 完成真实项目/合同数据映射
- [ ] 完成生产备份恢复演练
- [ ] 真实 canary/白名单验收

### Done

- [x] 搭建 WSL 开发环境（Node.js, Python, Docker, gh CLI, uv）
- [x] 克隆 it-spareparts 仓库
- [x] 恢复基线分支 `codex/maintenance-manager-combined`（HEAD `3dbc9dc`）
- [x] 推送基线分支到 GitHub
- [x] 同步服务器 codex 分支和 AI 资产到本地
- [x] 安装 Claude Code + OpenCode
- [x] 迁移 43 skills + 38 memory 文件
- [x] 同步 DeepSeek API 配置
- [x] 后端测试通过（2375 passed, 5 skipped）
- [x] 前端构建通过（tsc + vite build）
- [x] 建立 .ai/ 项目管理系统

### Blocked

- 🔴 无（#228 与 #227 已解决）
- 🟡 Durable #234：仍冻结，等待 Capability→Artifact 组合后按 `c4→ad8→b1→c2→d9` 线性重放

## Changed Files (本次会话 2026-08-11)

- `.ai/CURRENT_TASK.md` — 更新任务状态
- `.ai/CHANGELOG.md` — 记录 2026-08-11 会话
- `backend/app/agent/workbook_cleaning/{models,kernel,__init__}.py` — #228 两个 P1 修复
- `backend/tests/test_workbook_cleaning_proposal.py` — +3 回归测试
- `~/.bashrc`、git 全局配置 — mihomo 7897 代理持久化

## Next Step

1. 人工审查 #235 Capability Kernel（CI 已绿，等人工）
2. 在干净 worktree 线性组合 Capability #235 → Artifact #236（统一 config/model/fixture 重叠）
3. 从组合 head 重放 Durable #234（迁移链 `c4→ad8→b1→c2→d9`，复用 `agent_integrity.py`）
