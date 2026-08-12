# Changelog

> 记录每次 AI Agent 或开发者的代码变更。格式：日期 + Agent + 任务 + 变更文件 + 测试 + 备注。

---

## 2026-08-12

**Agent:** Claude Code (WSL Ubuntu 26.04)
**Session:** Plan-First 协议 + .ai/ 体系补全

**Changed:**
- `.claude/skills/plan-first/SKILL.md` — 新建 Plan-First 开发协议 skill（Claude Code 版）：问题/目标/路径/验收/风险 五段式计划模板 + 审批门禁 + 留痕闭环
- `.opencode/PLAN_FIRST.md` — OpenCode 版 Plan-First 协议
- `.codex/PLAN_FIRST.md` — Codex 版 Plan-First 协议
- `CLAUDE.md` — 新增 Plan-First Protocol 强制门禁段，引用 skill 文件
- `.ai/AI_WORKFLOW.md` — Phase 0 Step 4 改为五段式计划模板，引用 plan-first skill
- `docs/维保管理字段业务化改造方案.html` — 全量字段业务化改造方案（5 页面 + 全局术语）
- `.ai/CHANGELOG.md` — 本次变更记录

**Notes:**
- 三个 skill 文件为三平台独立副本，核心逻辑相同，格式适配各平台
- Plan-First 协议触发条件：>1 文件、新功能、Bug 修复、重构；例外：单行 typo、格式化、用户说"直接做"
- 同步完成架构文档审计：现有 .ai/ARCHITECTURE.md + DATABASE_DESIGN.md 中所有数字均与实际代码不符，需后续全量修复

---

## 2026-08-11

**Agent:** OpenCode (WSL Ubuntu 26.04)
**Session:** 网络恢复 + #227 找回 + #228 P1 修复

**Changed:**
- `~/.bashrc` + git 全局配置 — 持久化 mihomo 7897 代理（GitHub DNS 污染/直连超时，经代理恢复 gh/curl/git）
- 推送 3 个服务器独有分支到 GitHub：`codex/replenishment-cart`（`bce7deb`）、`codex/issue201-formal-review`（`eef2933`）、`codex/fix-maintenance-return-rate-lock`（`895637c`）
- **#227 找回** `8fd395a`：经 `cloudlay@ddns.cloudlay.cn` SSH 旧服务器，在 `/tmp/it-spareparts-artifact.m1cbgs` worktree 找到该 commit，bundle 传回并恢复分支；验证 68 tests + Ruff(0.14.6) + format + py_compile + diff check 全绿；推送 origin 后 PR #239 head = `8fd395a`，CI 全绿
- `backend/app/agent/workbook_cleaning/models.py` — **#228 P1-2**：新增 `ProposedValueSnapshot`；Assessment 容器（field_diffs/risk_flags/manual_review_reasons）镜像上限 + 唯一性；binding 模型加 max_length/唯一性；`change_count` 与 field_diffs 长度一致性校验
- `backend/app/agent/workbook_cleaning/kernel.py` — **#228 P1-1**：kernel 强制 `proposed_after` 与可信快照值一致（`proposed_value_mismatch`），拒绝未绑定/多余快照（`unbound_proposed_value_ref`/`extra_proposed_value_snapshot`）
- `backend/app/agent/workbook_cleaning/__init__.py` — 导出 `ProposedValueSnapshot`、`SourceEvidenceBinding`
- `backend/tests/test_workbook_cleaning_proposal.py` — +3 回归测试（快照绑定、快照覆盖、Assessment 容器上限）
- 创建 Draft PR #242（#228 修复），CI 前后端全绿

**Tests:** 33 focused tests 全绿（#228）；68 focused tests 全绿（#227）；PR #242/#239 GitHub CI 前后端全绿

**Notes:**
- ruff 0.16.2 对 #227 报 6 个 I001 为版本差异（服务器当时 0.14.6 通过），不改代码保持精确 SHA
- 旧服务器 tailscaled socket 在 `/tmp/tailscale.sock`；SSH 可用 `cloudlay@ddns.cloudlay.cn`

---

## 2026-08-11

**Agent:** OpenCode (WSL Ubuntu 26.04)
**Session:** 制定开发留痕协议（Traceability 硬性要求）

**Changed:**
- `.ai/DEVELOPMENT_RULES.md` — 新增第 0 章"开发留痕协议"：4 类痕迹（任务/变更/提交/架构）、可回溯判定标准、0.3 收尾自答检查（原有状态/变成什么/为什么/是否架构变动/影响面）、禁止项清单
- `.ai/AI_WORKFLOW.md` — Phase 0 Step 4 增加"原有状态登记"；Phase 2 更新文档步骤改为硬性留痕（CHANGELOG 必须含 before/after/原因/验证/commit SHA）；Handoff 协议增加"留痕缺失先补齐"条款
- `.ai/AI_REVIEW_CHECKLIST.md` — "Documentation" 拆分为 "Documentation & Traceability"（7 项硬性检查）；Git 部分增加 commit SHA 回填检查
- `CLAUDE.md` — 新增 "Traceability (hard requirement)" 章节（5 条），所有 AI Agent 在 After Coding 前必读

**Tests:** 无代码变更，纯规范文档

**Notes:** 规范生效后，任何无 CHANGELOG/ADR 记录的变更视为未完成，Code Review 打回。commit `4e5ae474`

---

## 2026-08-10

**Agent:** Claude Code (VSCode Extension, Windows)
**Session:** 开发环境迁移

**Changed:**
- 创建 `.ai/` 项目管理系统（14 个文件）
- 创建 `CLAUDE.md`（AI Coding Rules）
- 创建 `.env`（dev 环境配置）
- 更新 `backend/spareparts_backend.egg-info/SOURCES.txt`（uv sync 自动生成）
- 更新 `frontend/package-lock.json`（npm install 自动生成）

**Tests:** 后端 2375 passed / 5 skipped；前端构建通过

**Notes:**
- 基线分支 `codex/maintenance-manager-combined` 已推送 GitHub（HEAD `3dbc9dc`）
- GitHub `workflow` scope 已授权
- 从旧服务器同步了 4 个 codex 独有分支 + worktree refs + .codex AI 资产
- 安装 Claude Code 2.1.226 + OpenCode 1.18.16
- 迁移 43 skills + 38 memory 文件到 WSL
- 旧的交接包 zip SHA256 验证通过（`d5b21765...c2f06`）

---

## 2026-08-09

**Agent:** Claude Code (cloudlay-ubuntu server)
**Task:** 开发交接打包 (#240, #241)

**Changed:**
- `docs/handoff/2026-08-10-development-handoff.md` — 开发交接文档
- `docs/handoff/SECRETS-AND-SERVERS.md` — 安全交接说明
- `3b4af2a` docs: record GitHub handoff status
- `44458dc` docs: add development handoff package

**Tests:** 完整通过（2754 passed, 5 skipped, 登录构建绿）

---

## 2026-07-30 — 2026-08-09

**Agent:** Claude Code (cloudlay-ubuntu server, multiple sessions)
**Task:** 维保 Beta 集成组合 (#204 #205 #206 #207 #208 #209 + replenishment + migration)

**Changed:**
- 107 commits on `codex/maintenance-manager-combined`
- 维保项目工作台、工作簿 v3、验收、现场领用、坏件返还、WBDD 删除、成本迁移、补库购物车
- Beta 白名单、总闸控制、v1.21 发布控制
- 7 个 merge migration 保持单 head

**Tests:** 维护阶段全量绿

---

## 2026-07-15 — 2026-07-26

**Agent:** Claude Code (macOS + cloudlay-ubuntu)
**Task:** AI 助手 P0-P1 + 项目合同基础 (#196 #198 #200)

**Changed:**
- `backend/app/agent/` — AI Chat 引擎（6 文件）
- 10 个 Agent 工具 + 4 个 Skill Playbook
- Vision OCR 文件识别
- Chat 持久化（PostgreSQL）
- 维保项目主档与多合同聚合
- 项目操作工作台

**Tests:** Agent 5 个测试文件 + 维保核心测试

---

## Template (for future use)

```markdown
## YYYY-MM-DD

**Agent:** [Claude Code / OpenCode / Cursor / Human]
**Task:** [简短描述]

**Changed:**
- `path/to/file` — 变更说明

**Tests:** [通过/失败/X passed Y skipped]

**Notes:** [注意事项]
```
