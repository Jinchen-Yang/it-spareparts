# 交接：维修循环池与闲鱼上架（D-0057）

> 日期：2026-11-10 19:40（北京时间）。接手者先读 `AGENTS.md`，再读 [D-0057](../decisions/0037-坏件循环池与闲鱼可售意图.md)、[需求00014](../requirements/需求00014.md)、[投递契约 §10](../integrations/cycle-sell-intent-v1.md)、[工位页计划](../plans/2026-11-10-循环池工位页与入站.md)。
>
> 本文是本会话的记忆，不是新口径。口径以 D-0057 为准。合并不等于上线。生产仍是 `e62cd60`（v1.46.0）。

## 1. 两个仓库现在能看到什么

| 仓库 | 这次能否打开 | 上架结论 |
|---|---|---|
| `Jinchen-Yang/it-spareparts` | 能。当前 GitHub 身份只能列出这一个仓库 | 闲鱼上架没有实现。未合并分支只把「一件货」投递给商城 |
| `Jinchen-Yang/spareparts-shop` | 不能。仓库页和 API 都是 404 | 合帖、闲管家客户端、上架代码这次没有读到。不能把 404 说成仓库已删除 |

2026-11-08 写在契约附录 A 的商城待核（基准 `origin/main = 7723be7`）是旧记录，不是这次打开商城核对的结果。那次记录是：没有 `sell-intents` 路由，闲管家创建、改库存、下架、重上架为 0/4。若该记录仍有效，商城收到意图后也只能停在 `publish_api_missing_stock_update`。下次编码商城前，必须用能读商城仓的身份再对一次 head。

## 2. itsp 分支

`origin/main` 在交接时是 `b877fecf`（#526）。`main` 上没有 `SellIntentWorker`，也没有闲管家调用。

| PR | 分支 / head | 状态 | 内容 |
|---|---|---|---|
| [#527](https://github.com/Jinchen-Yang/it-spareparts/pull/527) | `feat/cycle-pool-t0-docs` `bacdb113` | OPEN，相对 main 可合并，必需 CI 曾双绿 | D-0057、需求00014、CONTEXT。无代码 |
| [#528](https://github.com/Jinchen-Yang/it-spareparts/pull/528) | `feat/cycle-pool-backend` `e7e74436` | OPEN，可合并，必需 CI 双绿 | 迁移 `86f7486b9ed5`、15 个 `/api/cycle`、待办、出站 worker、契约。无前端。#527 不是它的祖先 |
| [#529](https://github.com/Jinchen-Yang/it-spareparts/pull/529) | `cursor/cycle-pool-surface-9cc2` | OPEN，叠在 #528 上 | 入站回调、主管投递查询、工位页。本交接写在这条分支上。写交接时前端构建已通过，后端 pytest 仍在跑 |
| #520 #521 #522 | HOLD | 落后 main 或冲突 | 不要合并。#520 的决策号和迁移号已过时；#521 #522 的 diff 含发布物料删除 |

建议合入顺序：#527，然后 #528，再把 #529 变基到 #528 的新头后合入。三支都未合。不要从这些分支部署。

## 3. 已经写死、按此做

- 一件一行。工位：回收、检测、维修、委外、待处置、已结案。结案去向：去电商、回公司、报废；售出后 outcome 为 `sold`。
- 成品入池，不自动上架。修好可以再测，也可以不测。委外回来五个去向手选，可以直接去电商。
- 不写 `inventory`、氚云、`maintenance_bad_salvage`。工人响应不含项目、供应商、SN。
- 价格只在商城。itsp 不出价，不用成本填闲鱼价。
- 合帖推荐默认是 `(partId, conditionBucket)`。同一 PN 好坏分两帖。只有用户在商城合帖开工前明确说「一类 = `category_major`」才改商城合帖键。itsp 出站字段不用改。
- 四个发布开关默认全关。代码合并和口头开工都不打开 `SHOP_CYCLE_SELL_ENABLED`。
- 五个 `page_cycle_*` 默认全关，含 admin。菜单用 `accountScoped`，admin 角色不能绕过。

## 4. itsp 已编码的边界

#528 出站：`SellIntentWorker` 在开关和 URL 都配置时，向 `POST /api/integrations/cycle/sell-intents` 发送 `schemaVersion`、`deliveryId`、`unitId`、`partId`、`conditionBucket`、`qty=1`。默认不启动网络循环，意图保持 `pending`。这不是闲鱼上架。

#529 入站（契约 §10，不新增表）：

| 路径 | 行为 |
|---|---|
| `POST /api/integrations/cycle/requeue` | 凭据是 `CYCLE_SELL_CALLBACK_TOKEN_SHA256`。空则 404。`blocked` 只把 `available_at` 调到现在 |
| `POST /api/integrations/cycle/mark-sold` | 同一 `shopOrderLineId` 第二次 200 且不写第二笔。人工已售但事件里没有该行号时 409 |
| `POST /api/integrations/cycle/revert-sold` | 只把 outcome 从 `sold` 改回 `to_shop`，工位保持 `closed` |
| `GET /api/cycle/units/{id}/sell-intent` | 仅 `page_cycle_manage`。响应等于 `read_status()` |

工位页是 `/maintenance/cycle`。按钮只提交既有 `/api/cycle` 动作。

## 5. 还没有做

商城接收与合帖账、类目映射、标价、闲管家客户端、真实上架、订单售出分配、对账、打开 live、生产发布。#349 的循环档案「强制上架」和 #494 的 PN 目录同步已经在 main 上，它们不创建闲鱼商品。

## 6. 本地验证

写 #529 时：

```bash
cd backend && DATABASE_URL="postgresql+psycopg://spareparts:spareparts@127.0.0.1:5433/spareparts_test" \
  uv run --extra dev pytest -q tests/test_cycle_sell_inbound.py
```

8 passed。前端 `vitest` 覆盖导航门闩、回收岗自测不显示序列号、主管投递状态，以及 App 既有路由。

另建库 `spareparts_cycle` 迁到 `86f7486b9ed5`，种子账号 `admin` / `admin888` 被显式打开五个页键。浏览器打开 `/maintenance/cycle`，对 `MZ7LH960HAJR-00005` 点「自测通过」：单位从 `pool` 到 `ready`，`condition_bucket=good`，版本 2。`spareparts_dev` 当时停在旧迁移 `a8c2e4f6b0d1`，没有对它执行 upgrade。

## 7. 接手时不要做的事

- 不合并 #520 #521 #522。
- 不部署，不改生产，不打开出站开关和商城 live。
- 不在 itsp 里实现商城合帖或编造闲管家参数。
- 不把「一类一贴」再问一遍。用户没说 `category_major` 之前，按 PN 加好坏分帖。
- AI 不写循环工位、outcome、待办或可售意图。
