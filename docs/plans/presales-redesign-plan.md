# 售前询价/报价重设计 · 实施方案（交接用）

> 给接手的开发者或模型。待办与状态见 [presales-redesign-todo.md](presales-redesign-todo.md)，口径见 [D-0046](../decisions/0026-售前报价长期有效与主动报价简化.md)。
> 开工前按 `AGENTS.md` §2 读：`docs/decisions/` → `CONTEXT.md` → 本文件。

## 0. 技术栈与原型说明

- **正式代码**沿用仓库现有技术栈，不换框架（AGENTS.md §6 红线：不做整体重写）：
  - 前端：React 18 + TypeScript + Vite + Ant Design，测试用 vitest
  - 后端：FastAPI + SQLAlchemy + Alembic + PostgreSQL，测试用 pytest
- **设计原型**在 `docs/plans/presales-prototype/*.dc.html`，是 Artifact 画布的设计组件格式（纯 HTML + 一段 JS），**不能直接运行，也不能直接拷进项目**，只用来对照布局和交互：
  - `R.dc.html`：**推荐方案**，以它为准
  - `Main.dc.html` / `B.dc.html` / `C.dc.html`：早期的三个方向，仅供参考
- 在线查看原型：https://claude.ai/artifact/EPrRBaxJHWJnbsa18SLHum （私有，需要所有者账号或分享）。
- 用 AntD 实现原型：可编辑行用 `Table` 列里直接放 `Input` / `InputNumber` / `Select` / `Checkbox`（受控，状态放在页面）；「更多」折叠用 `Table` 的 `expandable` 或行内 `Collapse`；不再用 `Drawer` 做逐行编辑。

## 1. 分支与 PR 顺序

0. **PR #492**（已完成，CI 绿，待验收）：取消有效期。先合并它，再从最新 main 开新分支。
1. **PR-A 缺陷修复**（最急、最小）：沿用选不了、PN 新建不同步、测试账号领单。
2. **PR-B 后端口径**：主动报价＝更新报价、报价与询价状态脱钩、默认含税、行答复读模型。先写好 D-0046 剩余条目。
3. **PR-C 前端重排**：直接填写、原地更新报价、询价人前置、报价列、图片标识、快速建档。
4. **PR-D 新功能**：优先级、催办、坏件是否返回、成本价/售价。口径定下后再做，每项都要新决策编号。
5. **另开**：库存三库口径（非售前模块）、智能体提速。

每个 PR 的描述都要写 `决策: D-NNNN`（CI 会校验这一行）；本地跑 pytest、vitest、`tsc && vite build` 后再推。

## 2. PR-A 缺陷修复

| 问题 | 先查哪里 | 方向 |
|---|---|---|
| 沿用下拉选不中候选 | `frontend/src/pages/presales/QuoteGrid.tsx` 第 78–80 行：`Select` 的 `options.label` 里嵌了 `OfferBadge`（Tag，且内部有定时 hook）；`onChange` 走 `onResponse`，再到 `ProcessPage.tsx` 的 response 状态 | 先用生产数据复现。重点核对 `value` 与 `options.value` 的类型是否一致（number / string），以及 `onResponse` 是否被 `responseDisabled` 拦截。label 改成纯文本或用 `optionRender`，避免交互元素吞掉点击 |
| 新建 PN 后不同步（H6Z27A） | 先确认「同步」指什么：shop 全量同步（`backend/app/services/master_edit.py` 第 79 行附近），或检索、图库、AI 建档等下游。查定时任务配置和失败日志 | 修复失败原因；新建 PN 成功后立即投递一次增量同步（事件或任务队列），按小时、每日的批量同步保留作兜底 |
| 「内测-管理员2」领走生产单 | `backend/app/services/presales/candidates.py`（候选人）、`backend/app/services/workflow/work_items.py`（领取） | 测试账号不进候选、不能领取生产单据（按账号标记或角色过滤）；已领走的单据由管理员改派回公共池。是否允许他人只读查看被领取单据，待拍板 |

## 3. PR-B 后端口径（`backend/app/services/presales/`）

1. **主动报价＝更新报价**（D-0046 第 2 条）
   - 新增「按型号发布或更新本人报价」接口，例如 `POST /presales/parts/{part_id}/my-offer`，请求体为 `{unit_price, is_tax_inclusive, ...}`。
   - 本人对该 PN 已有有效报价时，直接更新（复用 `offers.update_offer`）；没有时新建一条已发布报价。
   - 报价必须挂在某个单据上（`PresalesOffer.inquiry_id` 非空），所以需要一个「型号报价」承载单据：每人一张、系统内部隐藏，不进询价列表。若改为允许 `inquiry_id` 为空，则要做迁移，并改 `visibility.py` 的可见性（按报价自身规则判定）。**二选一，写进 D-0046 再动手。**
   - 不再生成 BJ 单据，不调用 `work_items.open_items(... "presales_offer_notice" ...)`（`offers.py` 的 `publish_proactive` 末尾）。
   - 旧的 `publish_proactive` 接口保留只读兼容，前端不再调用。
2. **报价与询价状态脱钩**（第 4 条）：`update_offer`、`withdraw`、`_publication_plans` 中对 `origin.status != "submitted"` 的限制去掉，改为只校验报价人本人和报价状态；关单记录仍钉在成交时的修订号。
3. **默认含税**（第 5 条）：`save_draft` 新建时 `is_tax_inclusive` 默认为 `True`；前端去掉「未声明」选项；存量数据不迁移。
4. **行答复读模型**（第 7 条）：在 `visibility.inquiry_view` 里为每一行产出 `answer`：`{state: quoted | skip | pending | superseded, unit_price, is_tax_inclusive, reporter, quoted_at, latest?}`。
   - 优先级：有效价 > 不报（不报作旁注）> 待报；多条有效价取最新一条。
   - 列表接口在摘要里带出每行 `answer`，前端「报价」列直接读它。
   - 进度（已回复几行）也从 `answer` 计算，不再统计待办数量。

每条都补 pytest，参考现有 `backend/tests/test_presales_r6_*.py` 和 `test_presales_r7_*.py` 的写法。

## 4. PR-C 前端重排（`frontend/src/pages/presales/`），以 `R.dc.html` 为准

- **新建 / 编辑询价**（`InquiryEditorPage.tsx`）
  - 询价行改成可编辑 `Table`：PN（`LivePartSearch` 单选）、描述（只读，自动带出）、数量（默认 1）、PN 一致（`Checkbox`，按本人上次选择预填，记在 localStorage）、备注、删除。
  - 期望交期、客户要求、行备注可见范围、处理人、报价可见范围、引用报价放进行展开区或单头「更多选项」，默认折叠。
  - 删除每行「编辑」按钮和 `Drawer`（第 265、273 行）。
  - 支持从 Excel 粘贴多行到 PN 列，复用现有导入的解析逻辑。
- **采购报价**（`ProcessPage.tsx`、`QuoteGrid.tsx`）
  - 报价行改成可编辑 `Table`。第一列为询价人（加粗）+ 单号/客户，其后是 PN/描述、数量、PN 一致、单价、含税（默认含税）、状态、操作（保存 / 不报 / 沿用）。
  - 货期、可供数量、所在地、供应商、货源、备注放进行展开区。
  - 已报价的行显示价格和「更新报价」按钮，点击后该行变为可编辑，保存调用 `updateOffer`。
  - 同型号在另一张未关询价上已有有效价时，行下方提示「另一单 ¥x（某某）· 沿用」。
  - 删除 `QuoteGrid.tsx` 第 60 行的逐行 `Drawer`。
- **主动报价**：报价页顶部「主动报价」按钮，展开一个行内小表单（PN、单价、税）→ 调用 PR-B 的新接口 → 停留在本页并显示结果；`OfferNewPage.tsx` 删除，或改为跳转到报价页。
- **询价列表**（`InquiryListPage.tsx`）
  - 新增「报价」列，读后端 `answer`，显示单价、含税、报价人；未报显示「待报价」，不报显示原因。
  - 新增缩略图列（复用 `components/gallery/PartThumb`），没有图时显示「无图」。
- **菜单**（`frontend/src/nav.tsx`）：售前只留「询价」「报价」；报价库、主动报价改为详情深链或去掉。可参考分支 `codex/presales-ui-draft` 中 nav 的改法。
- **快速建档**（`frontend/src/components/parts/PartRegistrationForm.tsx`）
  - 新增「快速模式」：只显示 PN（自动带入）和描述，其余字段折叠在「更多资料」里。
  - 售前入口默认使用快速模式；建档成功后把新 `part_id` 填回当前行。
- **主数据「待补全」筛选**（`frontend/src/pages/MasterDataPage.tsx` 加对应后端查询）：缺品牌、类别或图片的 PN，可按创建来源=售前过滤。是否本轮做待拍板。

每个页面都要改对应的 vitest 用例（`frontend/src/pages/presales/__tests__/`）。原来针对 Drawer 的断言改成针对行内输入框。

## 5. PR-D 新功能（口径定下再做，每项先写决策）

| 功能 | 建议数据位置 | 注意 |
|---|---|---|
| 优先级 | `presales_inquiry.priority`（例如 普通/加急/特急） | 采购队列按优先级、再按时间排序；需迁移 |
| 催办 | 复用 workflow 事件 + 通知 | 限制频率（例如同一单 30 分钟内只能催一次）；写入时间线 |
| 坏件是否返回 | 询价行字段 `return_defective: bool \| null` | 先确认放在单头还是行上 |
| 成本价 / 售价 | 报价字段 `price_basis: cost \| sale` | 成本价的可见范围要单独规定（建议只给采购和管理层看） |

## 6. 其他（非本模块）

- **库存只看三个库**（联强成品库、徐汇库、广州库）：属于库存统计口径，按 D-NNNN 先写决策，再改库存聚合的仓库过滤（不在售前代码中）。
- **智能体提速**：先测量（接口耗时、模型调用、检索），再优化；和售前 PR 分开做。

## 7. 验收清单

- 新建询价：不点任何「编辑」按钮就能填完多行并提交。
- 采购：在一屏内逐行报价、更新报价、不报、沿用，无需打开抽屉。
- 主动报价：保存后不出现新单据和待办，报价库里出现或更新这条价。
- 询价列表：直接看到每行报价和有无图片。
- 售前快速建档只填两项；主数据页能找到待补全的 PN。
