# DeepSeek Harness 企业定制实施计划（IT备件管理系统）

- 基线：`@deepseek-ai/dsh` 0.1.0-rc.7 · 源码排查副本 `dsh-source/` · 排查报告见 [DSH系统插件排查报告.md](./DSH系统插件排查报告.md)
- 日期：2026-08-19 · 状态：待评审

---

## 0. 调查结论（决策依据）

| 事实 | 来源 | 对定制的影响 |
|---|---|---|
| DSH 全部能力 = Cordis 组合行；定制三条正路：自建 preset（⑤）/ profile patch（③）/ profile 本地插件包（⑦） | dsh-base + dsh-web-app bundle、`~/.dsh/profiles/web/` | 全部定制无需 fork DSH 源码，升级安全 |
| 已有本地插件先例 `dsh-vislab`：host 半（tools/webServer/systemPrompt + ctx.effect）+ client 半（esbuild 打包 → `window.__ModuleLoader__.load`，`dsh.client.platform: web`），profile `package.json` link 安装 | `~/Code/DSHarness_plugins/packages/vislab` | itdata 插件直接复用该工程形态与构建脚本 |
| `webServer` 服务支持 exact/prefix 路由 + WebSocket upgrade + fallback | dsh-host-webserver/lib | 可在 DSH 端口内反代 `/itd/` → FastAPI 后端，iframe 嵌入面板（含 SSE 流） |
| settings 服务有完整读写 API（序列化写队列 + `settings/updated` 热更新）；`llm-pi-ai` providers 与 `agent-default-model` 均为 settings namespace | dsh-settings、dsh-llm-pi-ai、dsh-agent-default-model | 企业配置服务器下发 → 插件写 settings 即可锁定模型，无需改 DSH 包 |
| Web 层 `ui-settings-models`、`ui-model-selection` 是组合行，profile patch 可 disable | dsh-web-app bundle | "取消自定义模型"的 UI 面很简单 |
| IT 后端已有成熟 RBAC：`data_*`（字段级）/`page_*`/`action_*`/`own_customers_only`（行级）+ HMAC token 即时吊销 + `require_page/require_action/apply_field_visibility` | `backend/app/permissions.py`(1022行)、`security.py`、`auth.py` | **权限门不重建**——DSH 侧所有 DB/数据访问都带用户 token 走后端，复用整套体系 |
| 后端已有 agent 工具集先例（search_parts/get_inventory/write_excel/read_document 等 20 个工具，全部传 `UserContext` 权限过滤） | `backend/app/agent/tools.py`、`api/agent.py` | DSH 侧工具的权限过滤语义与之一致；部分查询可直接复用后端逻辑 |
| 前端是独立 React/Vite/AntD SPA（`baseURL:/api`，自有登录页/路由） | `frontend/src` | MVP 用 iframe 嵌入成本最低；原生化留作二期 |
| GitHub monorepo 不可达（连接重置），npm 包内是可读 ESM | 网络 + dsh-source | 不依赖上游源码即可完成全部定制 |

---

## 1. 目标架构

```
┌─ 浏览器 ──────────────────────────────────────────────┐
│  DSH Web UI (127.0.0.1:3080)                           │
│  ├─ 原有会话/工具 UI（裁剪后 preset）                    │
│  ├─ [itdata client] 数据面板入口（iframe /itd/）         │
│  │   └─ IT 前端 SPA（vite base=/itd/ 构建）              │
│  ├─ [itdata client] 登录卡 / 权限管理面板 / 模型状态      │
│  └─ Agent 会话（模型 = 锁定的企业模型）                   │
└──────────────┬─────────────────────────────────────────┘
               │ /api（DSH gateway）+ /itd/*（反代）
┌─ DSH Host 进程 ────────────────────────────────────────┐
│  ├─ [itdata host] 登录桥（按会话存用户 token）            │
│  ├─ [itdata host] DB 工具：db_query / db_schema /       │
│  │     run_script（py）——全部经权限门                    │
│  ├─ [itdata host] /itd/ 前缀反代 → FastAPI :8000        │
│  ├─ [itdata-config host] 启动时从企业配置服务器拉模型配置  │
│  │     → 写 settings(llm-pi-ai + agent-default-model)   │
│  └─ profile patch：disable 模型选择 UI / telemetry 钉死  │
└──────────────┬─────────────────────────────────────────┘
               │ 用户 token（HMAC，随权限即时吊销）
┌─ IT 后端 FastAPI :8000 ───────────────────────────────┐
│  既有 RBAC（data/page/action/row）+ /agent/* 工具端点    │
└──────────────┬─────────────────────────────────────────┘
               │ SQLAlchemy（读写）/ 受限 DB 角色（脚本）
        PostgreSQL :5433
```

**核心原则：DSH 进程永不持有超权 DB 凭据。** Agent 的每一次数据访问都携带当前登录用户的 token（API 路径）或使用该用户权限映射出的受限 DB 角色（脚本路径），权限变更随 token 吊销即时生效。

---

## 2. 四个交付物的设计

### 2.1 插件 A：`dsh-itdata`（host + client，核心插件）

**登录桥（权限门的地基）**
- client：登录表单卡（Slot），提交到 host → host 经反代调 `POST /api/auth/login` 换 token
- host：token 按 DSH 会话 id 键控存储（含过期提醒）；提供 `itdata.auth` 内部服务
- 未登录时所有 itdata 工具返回"请先在面板登录"，绝不降级为匿名

**DB 访问工具（模型可见，全部过权限门）**

| 工具 | 路径 | 权限语义 |
|---|---|---|
| `db_schema` | 读后端维护的表结构快照（只读、无敏感数据） | 登录即可 |
| `db_query`（text2sql 执行口） | agent 写 SQL → host 调后端新增只读端点 `/agent/sql`（`BEGIN READ ONLY` + `apply_field_visibility` 抹字段 + 行级过滤） | 用户 `data_*` 全量过滤 |
| `run_script` | 执行 py 脚本；脚本凭据 = 该用户映射的 PG 角色 DSN（只读角色 / 按模块授权的写角色），由权限管理面配置 | DB 级 grants |
| `call_api` | 通用只读业务查询：转发到既有 `/agent/*` 端点（复用 search_parts/get_inventory 等的权限过滤） | 既有 `page_*`/`data_*` |
| 写操作（更新/覆盖） | 不给 agent 通用写 SQL 口；一律走 `run_script` 跑预写好的脚本（仓库 `scripts/` 内白名单）或后端 `action_*` 门控端点 | 既有 `action_*` |

**面板嵌入（client）**
- MVP：host 注册 `/itd/` 前缀反代 → FastAPI；前端以 `base: '/itd/'` 单独构建产物由后端/DSH 静态托管；client 在 DSH 侧栏/overlay Slot 注册「数据面板」入口，内嵌 iframe
- 登录态打通：iframe 内是 IT 前端自己的登录页（token 存其 localStorage，与 DSH 侧 token 独立但同源账号）；MVP 不做 token 单点传递，二期再评估 postMessage 桥
- 二期（可选）：把老板看板/库存查询等 2-3 个高频页原生重写为 Slot 组件（弃 iframe）

**权限管理面板（client，仅 admin 角色可见）**
- 展示当前用户权限快照（登录后从 token/`/api/accounts/me` 拉取）
- admin 功能：DB 角色映射管理（IT 角色/权限组 → PG 角色与 grants 的映射表）、预写脚本白名单管理（存 `$DSH_HOME` or 后端）
- 映射数据落 IT 后端新表（`sys_db_grant_map`），DSH 侧只读消费 + 管理界面写后端 API

### 2.2 插件 B：`dsh-itdata-config`（模型锁定与远程下发）

- host only。启动（+ 定时，如 5min）从企业配置服务器 `GET /dsh/config` 拉取：
  ```json
  { "providers": { "enterprise-llm": { "api": "openai-completions", "baseURL": "...", "apiKey": "..." } },
    "defaultModel": { "provider": "enterprise-llm", "model": "..." } }
  ```
- 通过 settings 写 API 写入 `llm-pi-ai` 与 `agent-default-model` namespace（热生效）；apiKey 走 apiKeyEnv/credentials 引用而非明文 settings（按 dsh 惯例）
- 拉取失败：保持上次成功配置（本地缓存），首次失败则启动报错提示
- **UI 锁定（profile patch，不属本插件）**：disable `ui-settings-models`、`ui-model-selection` 行；模型固定为下发的唯一模型

### 2.3 企业 preset：`it-data`（复制 standard 裁剪）

落点 `~/.dsh/.agent-presets/it-data/`（用 `copy()` 起步）：

| 处置 | 行 |
|---|---|
| 保留 | tool-bash（shell + py 脚本=excel/word/pdf 处理通道）、tool-fs、tool-fs-search、tool-str-replace-editor、tool-jobs、tool-todo、tool-ask-user、tool-skill（企业技能挂载）、plan-mode、compaction 组、subagent（spawn，后台任务需要）、persona（企业定制文案） |
| 移除 | tool-web（无外网搜索）、tool-ralph、tool-workflow + workflow-worker-thread、tool-subagent-fork、tool-goal（按需）、tool-cordis（动态插件工具，锁死面） |
| 新增 | 无（插件 A/B 的行在 profile patch 层，宿主面） |

配套：`~/.dsh/.agent-presets/it-data/skills/` 放企业技能（excel/word/pdf 处理脚本规范、text2sql 规范、数据库 schema 说明）。

### 2.4 profile patch（`~/.dsh/profiles/web/cordis.patch.yml` 增补）

```yaml
# （示意，实施时按行 id 核对）
- id: ui-settings-models        # 模型设置页
  disabled: true
- id: ui-model-selection        # 会话内 /model 切换
  disabled: true
- id: session-telemetry-otel    # 遥测恒关（钉死，防环境变量误开）
  config: { mode: 'DISABLED' }
- id: agent-presets
  config: { default: it-data }
- insert:
    - id: itdata
      name: 'dsh-itdata'
      config: { backendUrl: 'http://127.0.0.1:8000', ... }
    - id: itdata-config
      name: 'dsh-itdata-config'
      config: { configUrl: 'https://企业配置服务器/dsh/config', pollMs: 300000 }
```

---

## 3. 实施阶段

| 阶段 | 内容 | 产出 | 验证 |
|---|---|---|---|
| **P1** 企业 preset | copy standard → it-data，裁剪行，写 preset.yml/persona/skills | `~/.dsh/.agent-presets/it-data/` | `standingKeyFor('it-data')` 挂载校验 + 真会话确认工具清单 |
| **P2** itdata 插件骨架 + 登录桥 | 插件工程（仿 vislab：esbuild client + host ESM）、/itd/ 反代、登录卡、按会话 token 存储 | `dsh-itdata` v0（link 进 profile） | DSH web 里登录/登出/过期重登；未登录时工具被拦 |
| **P3** DB 工具 + 权限门 | db_schema/db_query/call_api + 后端 `/agent/sql` 只读端点（含字段可见性/行级过滤） | 后端 PR + 工具上线 | 用不同权限账号对比同一 SQL 的可见字段；越权 SQL 被拒 |
| **P4** run_script + DB 角色映射 | 受限 DSN 分发、预写脚本白名单、权限管理面板（admin） | 脚本管线 + 映射表 | 只读角色跑 UPDATE 报错；白名单外脚本被拒；权限变更即时生效 |
| **P5** 面板嵌入 | 前端 `base:/itd/` 构建、iframe 面板 Slot、入口导航 | 数据面板进 DSH web | 面板内登录、看板/查询页可用、SSE 正常 |
| **P6** 模型锁定 | dsh-itdata-config + profile patch 锁 UI + telemetry 钉死 | 配置下发链路 | 改配置服务器 → 5min 内 DSH 生效；模型选择 UI 消失；抓包确认无遥测 |
| **P7** 收尾 | 端到端走查、文档（部署手册 + 权限模型说明）、settings.yaml 清理 | `dsh-enterprise/` 文档 | 全链路演示 |

依赖顺序：P1 独立可先行；P2→P3→P4→P5 串行（都依赖登录桥/插件骨架）；P6 独立可并行；P7 最后。
后端改动集中在 P3（一个只读 SQL 端点）与 P4（映射表 + 可能的脚本执行端点），均走 PR 流程（CLAUDE.md 规范）。

---

## 4. 已确认决策（2026-08-19）

1. **部署拓扑**：✅ 每人本机一份 dsh web（127.0.0.1），反代指向公司服务器上的 IT 后端。登录桥按单用户设计。
2. **面板嵌入 MVP**：✅ iframe 反代先行，高频页面原生化留二期。
3. **脚本写库边界**：✅ agent 临时脚本只拿只读 DB 角色；任何 UPDATE/INSERT/覆盖仅限仓库预写白名单脚本或后端 `action_*` 门控端点。
4. **模型配置服务器**：暂无现成服务 → P6 在 IT 后端加一个受 admin 保护的下发端点。

## 5. 风险

- DSH rc 版本迭代快（0.1.0-rc.7），preset/插件形态可能变 → 定制全部走 ⑤③⑦ 三条官方缝隙，不 fork 源码，升级时可逐项回归
- `/agent/sql` 只读端点是新攻击面 → 只读事务 + 白名单语句类型 + 行数/超时限制 + 复用 `record_access_log` 审计
- iframe 内 IT 前端改动（base 路径、CSP、cookie SameSite）需联调 → P5 单列验证
- 多用户共享拓扑（若选）下 host 内存 token 的会话隔离需按 DSH session id 严格键控，P2 设计时按共享标准做（本机拓扑天然兼容）

---

## 6. 执行状态（2026-08-19 晚）

| 阶段 | 状态 | 证据 |
|---|---|---|
| P1 企业 preset it-data | ✅ | `standingKeyFor` 挂载校验通过 |
| P2 itdata 插件骨架+登录桥+反代 | ✅ | 3081 实例 RPC 全链路（含错误路径透传） |
| P3 /agent/sql\|schema\|call + 3 工具 | ✅ | 后端 E2E 16/16 + 工具层 10/10 |
| P4 白名单脚本+DSN+管理面板 | ✅ | 后端 28/28 + 工具层 18/18 + 真实 dsh 进程 RPC 验证 |
| P5 面板 iframe 嵌入 | ✅ | /itd/ SPA+assets+fallback+反代登录全通（prefix 无尾斜杠坑已修） |
| P6 模型锁定+远程下发 | ✅ | 隔离 DSH_HOME 验证 settings 被锁定（llm-pi-ai 单 provider + agent-default-model 固定）+ 组合树 disable 生效 |
| P7 收尾 | ✅ | 部署手册完成、测试实例与临时库清理 |

**遗留（用户侧动作）**：重启本机 dsh web（3080）加载全部定制；8000 dev 后端重启以暴露新端点。
