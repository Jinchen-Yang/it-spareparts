# 循环池前端 + 闲鱼可售意图 · 架构与技术选型

决策: D-0057  
日期：2026-11-07（America/Los_Angeles）  
前置：后端表/状态机/HTTP 与前端 PRD 同在本分支（`cursor/cycle-pool-v2-cf8c`，接替 #520/#522）。  
本文只做**后续两块怎么建**的技术选型与架构 plan，不写业务代码、不授权生产部署。

## 0. 「两个新部分」是什么

| # | 部分 | 落地仓库 | 一句话 |
|---|---|---|---|
| A | 坏件循环**工位前端** | `it-spareparts`（本仓 `frontend/`） | 回收 / 检测 / 维修 / 主管 / WIP 只读条，调 `/api/cycle` |
| B | **可售意图投递 + 闲鱼上架** | 本仓出站 worker + `spareparts-shop` 消费与发布 | itsp 只通知；商城排队、闲管家自动发帖（同键单链接库存 1）、售出回写并重挂 |

边界（已拍板，不重开）：

- 氚云进销存零写入；不写 `maintenance_bad_salvage`。
- 目录同步（D-36 零库存 SKU）≠ 上架；上架只吃 sell-intent。
- 一类一贴键 = `part_id` + `conditionBucket(good|bad)`，不是大类。同键在闲鱼同时只挂一条、库存 1；售出后自动新挂下一条（2026-11-08 拍板）。
- 发布通道 = 闲管家，商城自动发布，员工不手动发布。
- 全自动上架：缺图 / 缺类目 → `blocked`，补齐后续跑，不回人工草稿确认页。
- 跨仓需求不自动立项：商城侧实现须管理员确认（`docs/BRANCH_POLICY.md` / 需求台账双仓规则）。
- 历史 `test` 上的「备件循环」实验**不是**本方案，禁止混用其表与口径。

---

## A. 工位前端：技术栈与怎么开发

### A.1 选型结论（推荐）

**继续本仓现有前端栈，不另起技术栈、不拆独立 SPA。**

| 层 | 选用 | 理由 |
|---|---|---|
| 运行时 | React 18 + TypeScript 5.6 + Vite 7 | 与全站一致；权限、登录、axios 拦截器现成 |
| UI | Ant Design 5.21 + `@ant-design/icons` | 工位队列/抽屉/表格与售前工作台同构 |
| 路由 | `react-router-dom` 7 + `nav.tsx` 登记 | `page_cycle_*` 可直接挂 `perm` / `anyPerm` |
| 测试 | vitest + testing-library | 契约：工人账号响应无供应商列等 |
| 视觉 | **另案设计**（你方出视觉/IA） | 实现仍落本仓组件；不因视觉另开 Next/独立站 |

不推荐：Next.js 新站、微前端、原生 App、在维保 `page_maintenance` 下硬塞子页。  
原因：五套页键与字段裁剪依赖本仓权限快照；另站要重做鉴权与菜单，收益小、边界事故成本高。

### A.2 信息架构 → 代码落点

```
frontend/src/
  pages/cycle/
    RecycleWorkbenchPage.tsx   # page_cycle_recycle
    TestQueuePage.tsx          # page_cycle_test
    RepairQueuePage.tsx        # page_cycle_repair
    ManageBoardPage.tsx        # page_cycle_manage
    components/
      UnitTable.tsx            # 列表列按 manage 开关裁剪
      DispatchDrawer.tsx
      CloseOutcomeForm.tsx     # to_shop 必选成色桶
      EventTimeline.tsx        # 仅 manage / 精简工人版
    api.ts                     # /api/cycle/* 封装 + expected_version
  components/cycle/
    WipByPartStrip.tsx         # page_cycle_wip；嵌入采购/售前 PN 旁，不改型号全景
```

`nav.tsx` 建议独立导航组「坏件循环」，五项分别绑五键；**不要**挂在维保组下。  
参考实现模式（只借交互骨架，不抄业务）：

- 队列：`pages/presales/PoolPage.tsx`
- 带版本 CAS 的写操作：`pages/presales/ProcessPage.tsx`
- 治理队列抽屉：`pages/governance/DataQualityIssuesPanel.tsx`

### A.3 前端架构要点

1. **权限即视图**：无 `page_cycle_manage` 时表格/详情**根本不渲染**供应商、来源单、SN；不依赖「有字段再藏」。后端已裁剪，前端再挡一层。
2. **乐观锁**：所有写带 `expected_version`；409 → 拉详情刷新再操作，禁止静默重放。
3. **去电商 UX**：成功 toast「已提交自动上架」即止；无二次确认、无闲鱼预览嵌页（PRD §4.6 / §7）。
4. **WIP**：`GET /cycle/wip?part_ids=` 批量；无权限整块隐藏；有权限且数量为 0 可显示 0，勿把「无权限」画成 0。
5. **状态机按钮**：只暴露当前工位合法转移（与后端错误对齐）；委外回来五选一手选，无默认。
6. **开发节奏**：先接 OpenAPI/现网 `/api/cycle` 契约测通 → 按工位拆 PR（回收 → 检测/维修 → 主管 → WIP 嵌入）；视觉稿可并行，不阻塞 API 接线。

### A.4 本仓前端不做

- 闲管家后台、类目映射、图库上传（商城）
- 工单系统、测报 PDF、SN 扫描主路径
- 把在制数写入 `/parts/overview`

---

## B. 可售意图投递 + 闲鱼上架：技术栈与架构

### B.1 总览

```
[循环单位 close(to_shop)]
        │  同事务 enqueue
        ▼
maintenance_cycle_sell_intent (pending)
        │  itsp 出站 worker（仿 catalog_delivery）
        ▼
POST shop /api/integrations/cycle/sell-intents   (新契约)
        │
        ├─ 目录/SKU 已存在？否则依赖既有 catalog 同步，不在本意图里建主数据
        ├─ 缺正式图 / 缺闲鱼类目映射？→ 回执 blocked → intent.state=blocked；补齐后 itsp 退避重投
        └─ 否则 unit 进入商城队列 key=(partId, conditionBucket) → 回执 applied
                │
                ▼ 商城 listing 调度（每键同时最多 1 条在架）
        该键无在架链接 → 取队首 unit → 闲管家发布 1 件链接（库存 1，绑定该 unit）
                │
                ▼ 闲鱼成交（闲管家订单回传）
        商城回调 itsp mark-sold(unitId) → unit.outcome=sold
        该键队列仍有 unit → 自动新挂下一条；队列空 → 不挂
```

### B.2 itsp 侧技术选型

| 能力 | 选型 | 说明 |
|---|---|---|
| 出站存储 | 已有表 `maintenance_cycle_sell_intent` | 一 unit 一意图 |
| Worker | **复制 `catalog_delivery` 模式**（asyncio + `FOR UPDATE SKIP LOCKED` + 短租约 + CAS 回执） | 见 `backend/app/services/catalog_delivery.py`；DB 事务与 HTTP 分离 |
| 启动 | `main.py` 旁路再挂一个 `SellIntentWorker` | 默认可关；与目录投递互不影响 |
| 配置 | 独立环境变量（勿复用目录 token） | 建议：`SHOP_CYCLE_SELL_ENABLED`、`SHOP_CYCLE_SELL_URL`、`SHOP_CYCLE_SELL_TOKEN`（校验规则对齐 catalog：无查询串 URL、32–256 ASCII token） |
| 载荷 | camelCase JSON，最小字段 | `schemaVersion`, `deliveryId`, `unitId`, `partId`, `conditionBucket`, `qty` —— **禁止**项目/SN/供应商/成本/维修备注 |
| 状态 | `pending → delivering → delivered \| blocked \| failed` | `blocked` 为业务可恢复；网络类进退避重试，不伪造成功 |
| 售出入口 | 已有 `POST /api/cycle/units/{id}/mark-sold` | 后续可加服务 Bearer 专端，与用户 session 分离 |

**明确**：itsp **不**集成闲管家 SDK、不存闲鱼商品 ID 为主数据、不调闲鱼开放平台。

### B.3 商城侧技术选型

商城仓（本机 `spareparts-shop`）与本仓同构：后端 FastAPI + SQLAlchemy 2 + Alembic + PostgreSQL（psycopg3，Python 3.12，uv），研究队列 LangGraph，图片阿里云 OSS；前端 React 18 + Vite 7 + AntD 5 + TanStack Query。下列建议沿用其现有模式，立项后按 shop 仓 `docs/09` §0 对齐：

| 能力 | 建议 |
|---|---|
| 接收 API | 新路由，独立 Bearer（≠ 目录 sync token ≠ 图库 token） |
| 幂等 | `(deliveryId)` 唯一；同 delivery 重放返回相同回执，不重复入队 |
| 排队 | 新表「可售队列」：每 unit 一行，键 `(part_id, condition_bucket)`，按入队时间先进先出 |
| 在架链接 | 新表「闲鱼链接」：绑定一个 unit、闲管家商品 ID、状态；部分唯一索引保证每键同时最多 1 条在架 |
| 资料 | 标题/参数从目录镜像读；成色文案用 good/bad 桶映射到对客用语（禁止「维修」「翻新」等敏感词进对外帖） |
| 发布通道 | **闲管家开放平台**（商品创建/上下架/订单 Webhook，需 app_id/app_secret 等，凭据只在商城）；封装成 shop adapter，itsp 只认回执枚举 |
| 阻塞 | 无正式图或无类目映射 → 回执 `blocked` + 稳定错误码；补图/补映射后 itsp 重投即续跑 |
| 售出 | 闲管家订单回传 → 链接置已售 → 回调 itsp `mark-sold(unitId)` → 同键队列有件则自动新挂 |
| 风控 | 闲鱼打击重复铺货与多号群控（见 shop `docs/research/06-闲鱼平台约束.md`）：同键单链接天然避免同款重复挂；重挂复用资料但不跨账号铺同图 |

回执枚举建议（契约草案另文）：

| receipt | itsp 落态 | 含义 |
|---|---|---|
| `applied` | delivered | 已进入同键可售队列（是否立即挂出取决于该键有无在架链接） |
| `unchanged` | delivered | 幂等重放，未重复入队 |
| `blocked` | blocked | 缺图/缺类目等可恢复业务阻点 |
| `conflict` | failed（或独立 conflict，若表扩枚举） | 身份/人工保护冲突，需人处理 |

（若表枚举暂无 `conflict`，首版可将 conflict 映射 `failed` + 不可自动重试错误码，后续迁移扩展。）

### B.4 跨系统契约落点

建议新增（本仓草案，**同字节副本**日后进 shop）：

- `docs/integrations/cycle-sell-intent-v1.md` —— 请求/回执/错误码/超时/大小上限（对齐 catalog：15s、4KiB、不跟 redirect、不读代理环境）

与既有契约关系：

| 契约 | 关系 |
|---|---|
| resale-catalog-v1 / catalog 投递 | 保证 PN/SKU 资料与零库存壳存在；**不加二手可售库存** |
| gallery-v1 | 正式图判定权威在商城；blocked 条件之一 |
| cycle-sell-intent-v1（新） | 唯一驱动「可售数量 / 上架」 |

### B.5 开发切片（建议顺序）

1. **本仓**：sell-intent worker + 环境开关 + 契约测试（mock shop；未配置 URL 保持 pending）。  
2. **契约文档**双方评审（管理员确认 shop 立项）。  
3. **shop**：接收 + 可售队列 + blocked + 每键单链接调度（闲管家 adapter 先用假实现验调度）。  
4. **shop**：闲管家真实 adapter：发布、订单 Webhook。  
5. **售出回写 + 自动重挂** + itsp 对账抽检。  
6. 联调 / 点按验收；生产另授权。

---

## C. 协作与验收

| 角色 | 负责 |
|---|---|
| 本仓代理 / 后端 | worker、契约草案、cycle API 稳定性 |
| 你方（另案设计） | 工位视觉与交互稿；实现仍建议落本仓 React |
| 商城仓 | 接收、排队、闲管家发布、售出回写与重挂（需立项） |
| 用户 | 点按验收；生产 `release-` |

验收锚点（摘要）：

- 工人 Network 无 vendor/项目字段。  
- `to_shop` 必出意图且 payload 无敏感字段。  
- 同 PN 同成色两次去电商 → 闲鱼只有 1 条在架、库存 1；首件售出后自动挂出第二件，第二件售出后不再挂。  
- 缺图意图 `blocked`，补图后自动继续，无人工「确认上架」。  
- 全程氚云库存表行数/结存不变。

---

## D. 待拍板 / 待确认（不阻塞写 plan，阻塞编码 B 的 shop 部分）

1. 商城仓管理员确认「循环可售意图」立项与排期。  
2. 已定：用闲管家自动发布，员工不手动发布（2026-11-08）。仍待确认：闲管家开放平台账号与凭据、挂在哪个闲鱼号（个人号同时在售上限 50 件；公司货走个人号有经营性卖家认定风险，见 shop `docs/research/06`）。  
3. `blocked` 续跑：shop 主动 webhook itsp 重投 vs itsp 定时扫描 — 推荐 **shop 补齐后调 itsp「requeue」或 itsp 对 blocked 指数退避重试且 shop 幂等**。  
4. 前端是否坚持本仓 React：默认是；若要坚持独立站点，需另开决策（鉴权/菜单/发布成本）。

---

## E. 相关文件

- 决策：[D-0057](../decisions/0037-坏件循环池与闲鱼可售意图.md)  
- 后端方案 / 前端 PRD：同 PR `docs/plans/cycle-pool-*.md`  
- 目录出站范本：`backend/app/services/catalog_delivery.py`、`docs/releases/pn-catalog-delivery-runbook.md`  
- 边界：D-36、`docs/BRANCH_POLICY.md`  
- 契约草案（同分支）：[cycle-sell-intent-v1.md](../integrations/cycle-sell-intent-v1.md)
- 闲鱼平台约束调研：shop 仓 `docs/research/06-闲鱼平台约束.md`
