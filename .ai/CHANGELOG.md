# Changelog

> 记录每次 AI Agent 或开发者的代码变更。格式：日期 + Agent + 任务 + 变更文件 + 测试 + 备注。

---

## 2026-08-16

**Agent:** Claude Code (Claude Code on the web)
**Session:** 维保前端信息架构设计（分支 `claude/maintenance-frontend-redesign-vr72wy`，基线 `origin/main@4f8b688`）
**阶段:** Plan —— 仅产出设计文档，未修改任何代码

**Before:**
- 维保前端的改造依据只有两份文档：`docs/superpowers/plans/PR3-ux-refactor.md` 与
  `docs/维保管理字段业务化改造方案.html`，两者内容约九成是术语翻译（`项目经理→维保负责人`、
  `PN→备件型号`、`dry-run→预检不保存`）
- 没有任何文档定义信息层级、状态语义分类、以及业务事实的页面归属
- 结果：文案改完之后信息仍然没有逻辑

**Changed:**
- 新建 `docs/maintenance/frontend-information-design.md`（commit `cb5bc66`）
  - 诊断 6 个结构问题 D1–D6，逐项附代码证据行号
  - 定义信息四层：决断层 / 指标层 / 事实层 / 依据层，每层固定呈现规格
  - 定义状态语义六类：逾期 / 待办 / 正常 / 不适用 / 受限 / 阻塞，规定只有前两类使用暖色
  - 定义归属唯一规则 + 18 条业务事实的阶段归属表（修 D1）
  - 定义标签预算：项目卡最多 3 个状态位（修 D3）
  - 逐页给出「展示哪些信息（含数据来源字段与所属层）」+「有哪些按钮（含出现条件、
    点击后行为、权限）」，覆盖我的待办 / 项目总览 / 项目详情五阶段 / 侧栏导航
  - 形式为文字说明 + ASCII 结构图，不含代码

**原因:**
- 用户反馈「前端显示的信息很没逻辑」，要求在 plan 阶段先出设计
- 已有文档只覆盖文案层，缺少信息架构层的定义

**验证:**
- 不涉及代码，无测试与构建
- 三张 ASCII 结构图已按 CJK 双宽规则校验列对齐（68 / 64 / 70 列，全部一致）

**Notes:**
- 落地映射到 PR1/PR3 已列出的待建文件（`BusinessActionCard.tsx`、`TechnicalDetails.tsx`、
  `MaintenanceWorkflowNav.tsx`、`MaintenanceWorkDashboardPage.tsx`），不新增 PR
- 新增一项 PR3 未覆盖的改动建议：项目总览的「任务类型」9 选项下拉改为按紧急度筛选
- 需业务确认 4 项：指标层取哪三个数 / 阻塞类是否支持一键转派 admin /
  决断条优先级按截止日还是金额 / `not_counted` 是否永不需要人工干预
- 实现需等用户确认本设计后另起 PR

---

## 2026-08-13

**Agent:** Claude Code (WSL Ubuntu 26.04)
**Session:** 维保业务工作台重构 → 生产审查修复（分支 `codex/maint-workbench-refactor`，基线 `origin/main@caf4a973`）

**Changed（11 commits，58 files，+8751/−259）:**
- 导航重构：两个同名"维保管理"拆为"维保项目（旧版）/ 维保工作台 / 维保数据维护"三组，业务化标签；betaFeature 白名单门控原样保留
- 需求单项目范围隔离（PR2）：复用 main 的 `owned_project_ids`/`MaintenanceSourceOrderAssignment`，搜索 + 删除意图双重范围校验，越界 403
- 业务文案（PR3/PR6）：maintenanceLanguage 模块 + 卡片/进度条/成本回填/迁移页面业务化；`dry-run→预检`、`manifest→技术依据`、`回填→补录`
- 采购链只读面板（PR4）：稳定归属表优先、唯一名称兜底、ACTIVE_STATUS 过滤、定点金额序列化
- 氚云项目导入（PR5A/5B）：真实脱敏样表字段契约 + preview/apply API + 前端四步向导；全量行存储、幂等 apply、409 冲突、流式 10MB 上限
- 独立审查 23 项发现全部修复：router 未注册、xlrd 生产依赖、迁移 revision 冲突（f9b2d4e7c1a6）、预览截断、apply 幂等、display_name 泄露、无界读取、fb token、N+1、作废单过滤
- `.deploy/v122_release.sh` + `v122_manifest.py`：发布/回滚控制（FROM d9f1a3c7e5b2 → TO f9b2d4e7c1a6），rollback-app 保留回滚排练门

**验证：**
- 前端 790/790 tests 绿、tsc + vite build 绿
- 后端 pytest 全量（等结果）+ 新增 11 个专项测试（范围隔离 7 + 导入契约 4）
- 迁移单 head：f9b2d4e7c1a6

**Notes:**
- 生产发布门：合并后需以 exact-SHA 建发布候选，执行 v122 preflight → backup-restore → migrate → deploy → observe，回滚走 rollback-app（旧镜像需先在 f9b2 上排练）
- 遗留：#128 待办页/验收矩阵完整版（PR6 余项）随真实账号验收推进

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
