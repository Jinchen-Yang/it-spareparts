# DSH（DeepSeek Harness）系统插件排查报告 — 企业定制前置分析

- **版本基线**：`@deepseek-ai/dsh` **0.1.0-rc.7**（Cordis 4 运行时）
- **源码副本**：本仓库 `dsh-source/`（195 个 `@deepseek-ai/*` 包，未压缩带注释的可读 ESM JS + `.d.ts` 类型；`node_modules/` 已被 .gitignore 覆盖，仅清单文件入库）
- **本机运行形态**：`dsh web`（Web GUI，127.0.0.1:3080），默认 preset = **standard**，默认模型 = `zai-coding-cn / glm-5.3`
- **排查日期**：2026-08-19

---

## 1. 组合层次：改哪里、怎么生效

DSH 的全部能力都是 Cordis 组合里的插件行。本机 Web 进程的组合栈自底向上：

| 层 | 文件位置 | 内容 | 可写性 |
|---|---|---|---|
| ① 基座 bundle | 包内 `@deepseek-ai/dsh-base/cordis.patch.yml`（451 行） | 宿主面（Host plane）：注册表、沙箱/审批、持久化、模型路由、全部工具行 | 部署只读 |
| ② Web bundle | 包内 `@deepseek-ai/dsh-web-app/cordis.patch.yml`（424 行） | Web 传输层 + 浏览器 UI roster；**将工具行 disabled，移交 preset** | 部署只读 |
| ③ Profile 用户层 | `~/.dsh/profiles/web/cordis.patch.yml` | 按 id 覆盖 ①② 行的 config / disabled / insert 新行（本机：VisLab） | ✅ 自由编辑 |
| ④ `--patch` 叠加 | CLI 参数，可重复 | 临时覆盖，优先级最高 | ✅ |
| ⑤ Agent preset | 包内 `dsh/config/agent-presets/<id>/`（shipped，只读，`system` 信任）与 `~/.dsh/.agent-presets/<id>/`（用户自建，shell 同级信任） | 每个 Agent/会话挂一份：工具、prompt 段、persona | ✅ 自建目录 |
| ⑥ 用户设置 | `~/.dsh/settings.yaml` | 模型 provider/密钥、默认 preset 等，**热更新** | ✅ |
| ⑦ Profile 插件 | `~/.dsh/profiles/web/node_modules` + `dsh plugin --profile web add <pkg>` | 安装自研/第三方插件包（本机已有 `dsh-vislab` link 先例） | ✅ |

**关键结论（定制生效路径）**：
- 改 `dsh-source/` 里的包源码 **不会生效** —— 它只是排查/参考副本。运行时从 `~/.dsh/profiles/node_modules` 解析 bundle。
- 行为定制的三条正路：**⑤ 自建 preset**（Agent 面，免重启免重建）→ **③ profile patch**（宿主面行覆盖，重启 `dsh web` 生效）→ **⑦ 同名包替换/链接**（深度魔改，需验证解析优先级）。
- Host plane vs Agent plane 判据（源码注释中明确）：被宿主行 `inject` 或被 Gateway Remote 反向解析的服务（如 `shell-env`、`goals`、`tasks`、`subagents`、`tokenMeter`）必须留在宿主面；preset 只贡献模型可见的工具与 prompt。

---

## 2. 系统自带插件清单

### 2.1 宿主基座层（dsh-base，~55 行）

**运行时内核**

| 行 id | 包 | 功能 |
|---|---|---|
| timer | cordis-plugin-timer | setInterval/setTimeout 注册表（可逆副作用） |
| hmr | cordis-plugin-hmr | 组合热重载（Web 层暂 disabled） |
| llm | dsh-llm | LLM 请求抽象 |
| session | dsh-session | 会话域 |
| typert / typert-loader / typert-gateway | dsh-typert-registry / -loader / dsh-api-gateway | 类型化 RPC 注册表 + Client→Host 网关 |
| agent / agent-loop / agent-default-model | dsh-agent / dsh-agent-loop / … | Agent 循环与启动 Agent 列表（默认空）；默认模型（默认 deepseek-official/v4-flash，本机被 settings 覆盖为 glm-5.3） |
| tools | dsh-tools | **模型工具注册表**（`mode` 可切 native/code/both） |
| system-prompt | dsh-system-prompt | persona 与 prompt 段组装 |
| shell-env | dsh-shell-env | 注入 `DSH_WEB_URL` 等环境变量到模型 shell |

**模型接入**

| 行 id | 包 | 功能 / 企业关注点 |
|---|---|---|
| llm-deepseek | dsh-llm-deepseek | DeepSeek 官方适配器（密钥经 credentials，不落盘于组合） |
| llm-pi-ai | dsh-llm-pi-ai | 多 provider 适配器（openai-completions 协议），**休眠挂载**：settings.yaml 写 `llm-pi-ai.providers` 才激活 —— 企业网关接入点 |
| llm-retry | dsh-llm-retry | 请求重试 |
| settings | dsh-settings-file | `~/.dsh/settings.yaml` 热更新文档 |
| credentials | dsh-credentials-local | 凭据解析：环境变量 > `.credentials.yaml` > 项目/用户 `.env` |

**持久化与检索**

| 行 id | 包 | 功能 / 企业关注点 |
|---|---|---|
| session-persistence-jsonl | dsh-session-persistence-jsonl | 会话日志（`$DSH_HOME/sessions`） |
| attachment-local | dsh-attachment-local | 图片等附件字节，内容寻址存会话外 |
| session-query-sqlite | dsh-session-query-sqlite | **全文会话检索默认关闭**（`openAt: never`，`:memory:`）；企业可开 `first-search` + 持久 path |
| session-projection | dsh-session-projection | 会话投影注册表（子代理目录等） |
| spill-local / spill-policy | dsh-spill-local / -policy | 大结果外溢（内联上限 50000 字节） |
| session-checkpoint-policy | dsh-session-checkpoint-policy | 每次模型请求前落检查点 |

**遥测 ⚠️**

| 行 id | 包 | 现状 / 企业定制点 |
|---|---|---|
| session-telemetry-otel | dsh-session-telemetry-otel | 默认 `DISABLED`。`DSH_TELEMETRY_MODE=FULL` 开启后上报 OTLP 到 **`https://harness-telemetry.deepseeksvc.com/v1/logs`**（可被 `DSH_TELEMETRY_OTLP_URL` 覆盖），携带匿名 user id。**企业合规：保持关闭，或 patch 该行 config 指向内网 collector / 环境变量钉死 DISABLED** |

**沙箱与审批**

| 行 id | 包 | 功能 / 企业关注点 |
|---|---|---|
| sandbox / fs-sandbox | dsh-sandbox-local / dsh-fs-sandbox | 文件效果边界；`cwd` 默认 process.cwd() |
| sandbox-policy | dsh-sandbox-policy | `DSH_PERMISSION_MODE`（默认 workspace-write），workspaceRoot=process.cwd() |
| bash-sandbox / pwsh-sandbox | dsh-bash-sandbox / dsh-pwsh-sandbox | shell 执行器（按平台二选一），超时 60s |
| approval | dsh-user-approval | 审批策略：`danger-full-access`→never，否则 ask（本会话即 never） |
| permission | dsh-permission-presets | 三档预设：read-only / workspace-write / danger-full-access —— **企业可裁剪预设表** |

**模型工具行（基座挂全量，Web 层统一 disabled 移交 preset）**

`tool-bash`、`tool-pwsh`、`tool-jobs`（后台任务控制）、`tool-fs`、`tool-fs-search`、`tool-str-replace-editor`、`tool-skill`、`tool-goal`、`tool-todo`、`tool-web`（**默认 fetch=false 仅搜索**，DeepSeek 搜索走 DEEPSEEK_API_KEY，60s 超时）、`tool-ask-user`、`tool-subagent`（spawn/fork 两种配置）+ `tool-subagent-control`（send_message/list_agents）+ `tool-subagent-report`、`tool-workflow`、`tool-ralph`（上限 64 轮）、`tool-cordis`（动态 Cordis 插件工具，宿主面注册）

**会话机制**

`plan-mode`（计划模式 prompt 段）、`compaction-basic` + `tool-result-pruner`（8192/4096/1024 裁剪参数）+ `command-compact`、`token-meter`（宿主面单例）、`goal` + `goal-round-driver` + `command-goal`、`repeat-tool-reminder`、`timeout-policy`、`agent-instructions`（maxBytes 65536，读 CLAUDE.md 类指令）、`commands` / `command-feedback`、`skill` / `skill-filesystem` / `skill-badge`(disabled)、`web` + `web-search-deepseek`、`session-title` + `session-title-llm`、`user-questions`、`subagent` + spawn/fork 后端

### 2.2 Web 层（dsh-web-app，~45 行）

**传输与服务**：`webserver`（默认 127.0.0.1:3080，`inject: [webStartup]`）、`web-runtime`（**`trustedHosts` 白名单 —— 企业内网部署定制点**）、`client-hmr`、`modules`（扫描浏览器 roster 进 `window.__DSH_BOOT__`）、`connection`（fetch/SSE，注入 webRuntime）、`api-remotes`、`client-runtime`、`cordis-host-runner` / `cordis-client-runner`、`code-runtime`（worker 线程）、`storage` / `storage-json`（`$DSH_HOME/storages`）/ `storage-domain`、`message-feedback`、`session-log-export`、`workspace`、`session-projection-cache`、`session-stats`、`directory-picker-auto`、`plugin-inventory`、`api-gateway`、`agent-presets`（roster，default: standard）

**浏览器 UI（`dsh.client` 行）**：`ui-theme`、`locale`、`ui-layout`、`ui-sidebar`、`ui-settings`(-general/-models/-plugin-inventory/-plugins)、`ui-conversation`、`ui-tool`、`ui-cordis`、`ui-workflow-run`、`ui-deliverables`、`ui-workspace`、`ui-input-trigger`、`ui-commands`、`ui-skill`、`ui-subagent`、`ui-jobs`、`ui-goal`、`ui-message-feedback`、`ui-model-selection`、`ui-permission`、`ui-agent-preset`、`ui-plan`、`ui-user-questions`、`ui-trajectory`

另有 `dsh-brand` 包（品牌/标题）与 `dsh-headless` bundle（`headless-startup` / `headless-runner` / `code-runtime`，供 `dsh --profile headless` 单任务模式）。

### 2.3 自带 Agent preset（4 个）

| preset | 名称 | 内容 |
|---|---|---|
| **standard**（本机默认） | 标准模式 | 完整编码 Agent：persona（`{{model}}`/`{{cwd}}`）、bash/pwsh、fs/fs-search、jobs、skill、goal、计划（isolate realm）、压缩组（compaction+pruner）、委派组（subagent/fork/workflow/ralph，codex/claude-code 后端 disabled 待启用）、ask-user、todo、web |
| code | PTC 模式 | standard 全量 + Code Mode SDK（工具以 TypeScript 程序组合，`DSH_TOOLS_MODE`） |
| minimal | 极简模式 | 仅持久 bash（pty/terminal-bash/persistent-bash/fs-local）+ str_replace_editor 双工具 |
| cordis | 创造模式 | standard 全量 + 运行时检查 + 插件实验 + preset 创作指导（自带 `skills/editing-cordis-compositions` 等） |

**Realm 规则**（preset 创作硬约束）：preset 内 provide 服务的行必须包在 `isolate` 组里；工具注册行（只注册不 provide）无需 realm。

### 2.4 本机已装定制（现状盘点）

- `~/.dsh/profiles/web/cordis.patch.yml`：插入 **VisLab**（`dsh-vislab`，link 自 `~/Code/DSHarness_plugins/packages/vislab`；跨目录文件工具 + 数理可视化面板），注册了 2 个 Obsidian 目录
- `~/.dsh/settings.yaml`：providers = `zai-coding-cn`（默认 glm-5.3）/ `ark` / `ark-coding` / `xiaomi-token-plan-cn`；`agent-loop.maxParallelToolCalls: 20`
- `~/.dsh/skills/`：6 个本地技能（easyeda-agent、frontend-design、mimo-transcribe-audio、requesting-code-review、systematic-debugging、verification-before-completion）
- 仓库技能与 arkcli 系列 skill 来自工作区/其他来源，随会话注入

---

## 3. 企业定制切入点（候选）

| # | 方向 | 落点 | 成本 |
|---|---|---|---|
| A | **遥测/外联合规**：确保 telemetry 恒关或转内网 OTLP；关闭 web_search（离线环境） | ③ patch `session-telemetry-otel` 行 config；preset 里去 `tool-web` 行 | 低 |
| B | **模型路由收口**：全部会话走企业 LLM 网关（openai-completions 协议），锁定默认模型，收编密钥管理 | ⑥ settings.yaml `llm-pi-ai.providers` + 企业 baseURL；③ patch `agent-default-model` | 低 |
| C | **企业 preset**：复制 standard → `~/.dsh/.agent-presets/<id>/`，裁剪工具面（关 ralph/workflow/web）、定制 persona 与提示词、内置企业规范文件（agent-instructions） | ⑤ | 低-中 |
| D | **审批与沙箱策略**：定制 permission 三档预设（如新增“仅内网”档）、默认 workspace-write、审批 ask/never 规则 | ③ patch `permission` / `approval` / `sandbox-policy` 行 | 低 |
| E | **内网部署**：bind host、`connection.trustedHosts` 追加企业域名 | ③ | 低 |
| F | **品牌/UI**：`dsh-brand`、ui-theme 主题色、侧栏/设置页定制 | 需替换包（⑦）+ 前端重建（浏览器侧是预构建 bundle，`dsh-source` 副本不含 monorepo 构建链；GitHub 当前不可达） | 高 |
| G | **自研宿主插件**：按 `dsh-vislab` 先例开发（技能库、企业系统集成、审计日志等） | ⑦ + ③ | 中 |

## 4. 风险与注意

1. **不要改部署自带的 preset 安装**（`dsh/config/agent-presets/`，随升级覆盖）——定制 preset 一律复制到 `~/.dsh/.agent-presets/<id>/`。
2. `dsh-source/` 为参考副本；用它 diff 出改点后，按第 1 节路径落地。
3. UI 深度定制（F）需要 GitHub monorepo 构建链，当前网络对 github.com 连接被重置，需镜像或离线传输。
4. 同名包替换（⑦ 用自建包遮蔽 `@deepseek-ai/*`）理论可行，但需先验证 profile node_modules 的解析优先级，未验证前不承诺。
