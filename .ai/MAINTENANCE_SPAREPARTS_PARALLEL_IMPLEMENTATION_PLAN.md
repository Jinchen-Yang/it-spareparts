# STOP — 本计划已被 V2 取代

> 2026-08-13 独立业务架构与数据库审查发现 P0，执行已暂停且未产生业务代码提交。后续只执行：
> `.ai/MAINTENANCE_SPAREPARTS_PARALLEL_IMPLEMENTATION_PLAN_V2.md`。
>
> 本文件仅保留为 rejected draft，任何 Agent 不得按下文创建 schema、migration 或 Lane worktree。

# 维保备件前置库双闭环 Parallel Implementation Plan（Rejected Draft）

> **For agentic workers:** REQUIRED SUB-SKILL: Use `dispatching-parallel-agents`, `test-driven-development`, `database-migrations`, and `verification-loop`. Use a serial implementer/reviewer pair for the shared kernel, then parallelize only the four file-isolated lanes defined here. The coordinating Claude Code session MUST explicitly start multiple implementation agents after the shared kernel is merged. Every agent works in an isolated Git worktree and owns only the files assigned below. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不改变旧系统默认行为、不删除任何生产数据的前提下，交付“购物车申请—实名审批”和“真实出库—现场收货—前置库存—领用核销—好件退回/坏件返还”的首个可灰度闭环。

**Architecture:** 先由唯一 Schema/Integration Owner 串行冻结稳定 ID、append-only 前置库事件账、API 契约、迁移和 feature flags；随后四条纵向 Lane 在独立 worktree 并行实现申请审批、现场收货、领用核销、返还政策；最后由 Integration Owner 串行挂载页面、合并、对账、发布。申请审批只形成供货许可，库存变化只来自现场实物事件；返还提交不恢复公司库存，只有关联已确认的正式入库单才闭环。

**Tech Stack:** Python 3.11+, FastAPI, Pydantic v2, SQLAlchemy 2, PostgreSQL 15, Alembic, pytest, React 18, TypeScript 5.6, Ant Design 5, Vitest, GitHub Actions, Docker Compose.

## Global Constraints

- 权威顺序固定为：用户最新业务更正 > `.ai/BUSINESS_PROCESS_MODEL.md` > 真实脱敏/受控生产样例 > 当前代码；研发不得补猜缺失字段。
- 开发基线固定为 `origin/codex/maint-workbench-refactor@c431656bd2615102f053199801554191b2d88791`；不得在落后 4 个提交的本地 `d7168ce3` 上实现。
- 新 Alembic revision 固定为 `a1d4e7f9c2b6`，`down_revision = "d9f1a3c7e5b2"`；不得以本地已失效的 `f9b2d4e7c1a6` 为父版本。
- 现有 `.ai/BUSINESS_PROCESS_MODEL.md` 与 `docs/superpowers/plans/PR7-release-plan.md` 是用户未跟踪文件；不得覆盖、删除或把失效 PR7 内容当发布依据。
- `申请通过 != 出库`、`出库 != 现场收货`、`现场收货 != 实际消耗`、`返还提交 != 公司库存恢复`。
- 项目、前置库、发货明细、PN、SN 只使用稳定 ID；项目名、仓库名、日期或模糊文本不得成为正式关联键。
- 所有库存写入只追加 `MaintenanceSiteStockEvent`；禁止直接写可变余额，禁止更新/删除历史事件，纠错只追加 reversal。
- 前置库守恒：`已收货 - 已领用消耗 - 已发出好件退回 + 已冲销 = 当前可用量`；坏件返还义务不进入这条新件库存公式。
- 备件成本只在 `site_issue_confirmed` 形成一次真实消耗；申请、发货、收货和前置库存占用不得重复计入项目消耗。
- 无可靠成本时返回 `null + evidence_state=unavailable`，禁止用 `0` 冒充真实零成本。
- 确定性错误由系统拦截；高成本、低频、跨项目余量、池价格越线只生成风险证据，由实名审核人裁决，首版不让 LLM 自动批准或驳回。
- 硬盘默认应返；只有有效的项目级返还政策和证据才能免返。未用的新硬盘始终走好件退回，不受坏件免返政策影响。
- 所有写接口必须同时具备总闸、实名账号白名单、action permission、项目行级权限、项目 allowlist、幂等键和乐观/行锁并发保护。
- 所有新增 feature flags 默认 `false`；flag 关闭时旧 API、旧页面和旧读口径必须保持不变。
- 生产数据只读用于样例核对；开发、测试、迁移演练不得连接生产数据库执行写操作。
- 生产迁移只允许 forward-fix；应用回切不得自动 downgrade 数据库，不得恢复旧 dump 覆盖已产生的新业务事实。
- 正式上线前必须完成数据库、上传文件、部署配置和当前镜像清单备份，生成 SHA-256，完成隔离恢复演练；任一失败立即停止发布。
- 任何 Agent 不得执行生产部署；只有 Root/Integration Owner 在全部门禁通过后执行。

---

## 1. 首版产品裁决

### 1.1 首版必须交付

```text
闭环 A：项目/前置库 → 购物车 → 证据冻结 → 提交 → 实名逐行审核 → 待执行供货意图

闭环 B：真实发货明细 → 现场实收 → 前置库可用 → 现场领用 → 实际消耗
                                              ├─ 未用好件退回 → 正式入库证据
                                              └─ 换下旧件 → 应返/项目免返 → 坏件返还
```

二者共享 `project_id`、`front_warehouse_id`、`part_id`、`delivery_line_id`、SN 和项目业务时间线，但首版不伪造“申请批准后已采购/已到货”的自动桥。

### 1.2 审批规则 V1

系统硬拦截码：

```text
PART_NOT_FOUND
PART_IDENTITY_AMBIGUOUS
PROJECT_INACTIVE
PROJECT_SCOPE_DENIED
FRONT_WAREHOUSE_MISMATCH
QUANTITY_INVALID
EVIDENCE_SNAPSHOT_FAILED
```

需要实名审核的风险码：

```text
NO_CONFIRMED_USAGE_180D
COST_SAMPLE_MISSING
POOL_PRICE_CEILING_EXCEEDED
OTHER_PROJECT_STOCK_AVAILABLE
PART_MASTER_REVIEW_REQUIRED
```

历史使用频次按提交日向前 180 天的 `confirmed` 现场领用行统计：`0=none`、`1..2=low`、`3..5=medium`、`>=6=high`。该标签仅供审核，不自动决定结果。

### 1.3 首版业务假设

- 现场收货使用 IT_data 原生网页表单。
- 试点阶段每个项目只允许一个 active 前置库；模型允许以后扩展多个。
- 换下旧件 V1 默认与领用新件使用同一 `part_id/PN`，但 SN 独立；跨 PN 更换失败关闭并提示走人工登记，不能静默猜测。
- 正式回库以已导入、`normalized_status=confirmed` 的仓库 `receipt` 单据及稳定行关联为证据；本期不直接修改 legacy `Inventory.source_qty/manual_qty`。
- 故障件维修、报废、变卖和贡献毛利止于外部边界，本期只记录去向，不制造虚拟采购/销售单。

## 2. 公共契约

### 2.1 数据对象

`backend/app/models/maintenance_site_stock.py` 由 Schema Owner 独占，定义：

```python
class MaintenanceFrontWarehouse(Base):
    front_warehouse_id: str
    project_id: str
    warehouse_code: str
    display_name: str
    status: Literal["active", "archived"]
    version: int
    created_by: str
    created_at: datetime
    updated_at: datetime

class MaintenanceSiteStockCommand(Base):
    command_id: str
    idempotency_key: str
    project_id: str
    action: Literal[
        "receipt_confirm", "consumption_confirm", "consumption_reverse",
        "good_return_dispatch", "good_return_reverse"
    ]
    request_fingerprint: str
    response_json: dict
    operated_by: str
    created_at: datetime

class MaintenanceSiteStockEvent(Base):
    event_id: str
    command_id: str
    project_id: str
    front_warehouse_id: str
    delivery_line_id: str
    part_id: int
    pn_snapshot: str
    serial_number_snapshot: str | None
    event_type: Literal[
        "receipt_confirmed", "consumption_confirmed",
        "good_return_dispatched", "reversal"
    ]
    quantity: Decimal
    aggregate_version: int
    source_type: Literal["site_receipt", "site_issue", "good_return"]
    source_id: str
    source_version: str
    reversal_of_event_id: str | None
    unit_cost_ex_tax: Decimal | None
    unit_cost_inc_tax: Decimal | None
    cost_evidence_json: dict | None
    reason: str
    operated_by: str
    occurred_at: datetime

class MaintenanceGoodReturn(Base):
    return_id: str
    return_no: str
    project_id: str
    front_warehouse_id: str
    status: Literal["draft", "submitted", "in_transit", "warehouse_confirmed", "void"]
    logistics_reference: str | None
    inbound_document_id: str | None
    reason: str
    created_by: str
    version: int

class MaintenanceGoodReturnLine(Base):
    return_line_id: str
    return_id: str
    line_no: int
    delivery_line_id: str
    part_id: int
    pn_snapshot: str
    serial_number_snapshot: str | None
    quantity: Decimal

class MaintenanceGoodReturnCommand(Base):
    command_id: str
    idempotency_key: str
    return_id: str
    action: Literal["create", "submit", "dispatch", "warehouse_confirm", "void"]
    request_fingerprint: str
    response_json: dict
    operated_by: str
    created_at: datetime

class MaintenanceProjectReturnPolicy(Base):
    policy_id: str
    project_id: str
    scope_type: Literal["category_major", "part"]
    category_major: str | None
    part_id: int | None
    decision: Literal["exempt"]
    evidence_reference: str
    effective_from: date
    effective_to: date | None
    status: Literal["active", "archived"]
    approved_by: str
    reason: str
    version: int
```

同一迁移以 additive 方式扩展：

```text
replenishment_application.project_id nullable FK
replenishment_application.front_warehouse_id nullable FK
replenishment_application.required_at nullable date
maintenance_site_issue_line.return_expected boolean default true
maintenance_site_issue_line.removed_serial_number nullable text
maintenance_return_obligation.serial_number nullable text
maintenance_return_obligation.policy_id nullable FK
maintenance_return_obligation.policy_evidence_reference nullable text
maintenance_bad_return_line.serial_number nullable text
```

历史记录保持原值，不在 schema migration 中推断项目、收货、SN 或免返政策。

### 2.2 Ledger Service

`backend/app/services/maintenance_site_stock_ledger.py` 对各 Lane 暴露且合入后冻结：

```python
def get_delivery_balance(
    db: Session,
    *,
    project_id: str,
    front_warehouse_id: str,
    delivery_line_id: str,
    lock: bool = False,
) -> dict

def append_site_stock_command(
    db: Session,
    *,
    project_id: str,
    front_warehouse_id: str,
    action: str,
    idempotency_key: str,
    request_payload: dict,
    mutations: list[dict],
    reason: str,
    operated_by: str,
) -> dict

def reverse_source_events(
    db: Session,
    *,
    project_id: str,
    source_type: str,
    source_id: str,
    source_version: str,
    idempotency_key: str,
    reason: str,
    operated_by: str,
) -> dict

def list_project_site_stock(
    db: Session,
    *,
    project_id: str,
    front_warehouse_id: str,
    q: str | None,
    page: int,
    page_size: int,
) -> dict
```

`append_site_stock_command` 必须：锁定全部 `delivery_line_id`（排序后 `FOR UPDATE`）、校验项目/PN/SN 来源、检测幂等重放、拒绝同 key 不同 fingerprint、计算 next aggregate version、阻止负余额、追加事件和 command receipt，并由调用者统一提交事务。

### 2.3 HTTP 契约

```http
GET  /api/maintenance/site-stock/projects/{project_id}
POST /api/maintenance/site-stock/projects/{project_id}/receipts
GET  /api/maintenance/site-stock/projects/{project_id}/reconciliation

POST /api/maintenance/good-returns/projects/{project_id}
POST /api/maintenance/good-returns/{return_id}/submit
POST /api/maintenance/good-returns/{return_id}/dispatch
POST /api/maintenance/good-returns/{return_id}/warehouse-confirm
POST /api/maintenance/good-returns/{return_id}/void
POST /api/maintenance/good-returns/search
```

收货请求：

```json
{
  "front_warehouse_id": "front-warehouse-uuid",
  "delivery_line_id": "stable-delivery-line-id",
  "quantity": "9.000",
  "mapping_version": "warehouse-projection-digest",
  "idempotency_key": "receipt-command-uuid",
  "reason": "现场清点后确认实收"
}
```

前置库明细响应：

```json
{
  "delivery_line_id": "stable-delivery-line-id",
  "part_id": 1001,
  "pn": "PN-001",
  "serial_number": null,
  "delivery_no": "OUT-001",
  "received_quantity": "9.000",
  "consumed_quantity": "3.000",
  "good_return_dispatched_quantity": "2.000",
  "available_quantity": "4.000",
  "oldest_receipt_date": "2026-08-13",
  "stock_age_days": 0,
  "unit_cost_ex_tax": "100.00",
  "occupied_amount_ex_tax": "400.00",
  "evidence_state": "confirmed"
}
```

正式回库请求必须提交 `inbound_document_id`，服务端验证它是 `document_type=receipt`、`normalized_status=confirmed`，并通过稳定 link 指向同项目和同 part/SN；不接受自由文本替代。

### 2.4 Feature Flags

`backend/app/config.py` 新增默认 false：

```python
maintenance_front_stock_shadow_enabled: bool = False
maintenance_front_stock_write_enabled: bool = False
maintenance_front_stock_read_enabled: bool = False
maintenance_front_stock_project_ids: str = ""
```

准入顺序：`MAINTENANCE_BETA_ENABLED` → 实名 `page_maintenance_beta` → action permission → `project_id` 行级 scope → `maintenance_front_stock_project_ids` → 对应 shadow/write/read flag。

## 3. 多 Agent 拓扑与文件所有权

```text
                 S0 Root Controller：计划、基线、生产边界
                                ↓
                 S1 Schema/Kernel Owner（串行、唯一）
                                ↓
        ┌────────────────┬────────────────┬────────────────┬────────────────┐
        │ Lane A         │ Lane B         │ Lane C         │ Lane D         │
        │ 购物车审批     │ 收货/前置库    │ 领用/消耗      │ 好件/坏件返还  │
        └────────────────┴────────────────┴────────────────┴────────────────┘
                                ↓
                  S2 Integration Owner（串行合并）
                                ↓
        Contract review → 全量测试 → 安全审计 → 迁移/恢复演练
                                ↓
       shadow → 命名项目 write canary → read canary → 0/5/15/30 → 次日对账
```

每个 Lane 使用独立分支和 `/tmp` worktree：

```text
codex/maint-spares-kernel       /tmp/it-maint-spares-kernel
codex/maint-spares-approval     /tmp/it-maint-spares-approval
codex/maint-spares-receipt      /tmp/it-maint-spares-receipt
codex/maint-spares-consumption  /tmp/it-maint-spares-consumption
codex/maint-spares-return       /tmp/it-maint-spares-return
codex/maint-spares-integration  /tmp/it-maint-spares-integration
```

禁止多人同时修改的文件：

| 文件 | 唯一所有者 |
|---|---|
| `backend/alembic/versions/a1d4e7f9c2b6_maintenance_site_stock_closure.py` | Schema/Kernel Owner |
| `backend/app/models/maintenance_site_stock.py` | Schema/Kernel Owner |
| `backend/app/models/replenishment.py` | Schema/Kernel Owner |
| `backend/app/models/maintenance_project_operations.py` | Schema/Kernel Owner |
| `backend/app/models/maintenance_bad_return.py` | Schema/Kernel Owner |
| `backend/app/models/__init__.py` | Schema/Kernel Owner |
| `backend/app/config.py` | Schema/Kernel Owner |
| `backend/app/permissions.py` | Integration Owner |
| `backend/app/main.py` | Integration Owner |
| `frontend/src/nav.tsx`、`frontend/src/App.tsx` | Integration Owner |
| `frontend/src/pages/maintenance/MaintenanceProjectWorkspacePage.tsx` | Integration Owner |
| `.ai/CHANGELOG.md` | Integration Owner |

公共契约需要变更时，Lane 不得自行修改 Kernel；必须在 lane report 中提出，由 Integration Owner 统一做一个契约版本提交，再 rebase 全部 Lane。

## 4. Task Plan

### Task 1: 创建精确基线和执行台账（Root Controller，串行）

**Files:**
- Read: `.ai/BUSINESS_PROCESS_MODEL.md`
- Read: `.ai/AI_WORKFLOW.md`
- Create outside Git: `.superpowers/sdd/maintenance-spares/progress.md`

**Interfaces:**
- Consumes: `origin/codex/maint-workbench-refactor@c431656b`
- Produces: 六个隔离 worktree、记录 branch/SHA/owner 的执行台账

- [ ] **Step 1: 核对远端与脏工作树**

```bash
git fetch --prune origin
git rev-parse origin/codex/maint-workbench-refactor
git status --short --branch
git rev-list --left-right --count origin/main...origin/codex/maint-workbench-refactor
```

Expected: feature SHA 为 `c431656bd2615102f053199801554191b2d88791`；原工作树用户文件保持不变。

- [ ] **Step 2: 创建 integration 和 kernel worktree**

```bash
git worktree add /tmp/it-maint-spares-integration -b codex/maint-spares-integration c431656bd2615102f053199801554191b2d88791
git worktree add /tmp/it-maint-spares-kernel -b codex/maint-spares-kernel c431656bd2615102f053199801554191b2d88791
```

- [ ] **Step 3: 建立台账**

台账首行写：

```text
# SDD ledger — plan: .ai/MAINTENANCE_SPAREPARTS_PARALLEL_IMPLEMENTATION_PLAN.md
```

随后记录 exact base SHA、worktree、Agent 名称、开始时间、commit、测试和 review 结论。

### Task 2: Shared Kernel、迁移与保护闸（Schema/Kernel Owner，串行）

**Files:**
- Create: `backend/app/models/maintenance_site_stock.py`
- Create: `backend/app/services/maintenance_site_stock_ledger.py`
- Create: `backend/app/maintenance_front_stock.py`
- Create: `backend/app/api/maintenance_site_stock.py`（仅 router 骨架）
- Create: `backend/app/api/maintenance_good_returns.py`（仅 router 骨架）
- Create: `backend/alembic/versions/a1d4e7f9c2b6_maintenance_site_stock_closure.py`
- Create: `backend/tests/test_maintenance_site_stock_logic.py`
- Create: `backend/tests/test_maintenance_site_stock_migration.py`
- Modify: `backend/app/models/replenishment.py`
- Modify: `backend/app/models/maintenance_project_operations.py`
- Modify: `backend/app/models/maintenance_bad_return.py`
- Modify: `backend/app/models/__init__.py`
- Modify: `backend/app/config.py`
- Modify: `backend/.env.example`
- Modify: `.env.example`
- Modify: `docker-compose.yml`

**Interfaces:**
- Consumes: stable delivery source `MaintenanceSiteIssueDeliverySource`
- Produces: 第 2 节全部 ORM、ledger 函数、flag helpers 和 router imports 可用

- [ ] **Step 1: 先写失败的 ledger 测试**

```python
def test_site_stock_balance_is_receipt_minus_consumption_and_good_return(db):
    receive(db, quantity="10.000", key="r-1")
    consume(db, quantity="3.000", key="c-1")
    dispatch_good_return(db, quantity="2.000", key="g-1")
    assert balance(db)["available_quantity"] == "5.000"
```

同时增加三个精确用例：同一幂等键换 payload 返回 `409 idempotency_conflict`；冲销后旧 event 行完全不变且余额恢复；两事务同时扣减时最多一笔成功且最终余额不小于 0。

- [ ] **Step 2: 运行并确认失败**

```bash
cd backend
uv run --extra dev pytest -q tests/test_maintenance_site_stock_logic.py
```

Expected: imports/models/functions 尚不存在。

- [ ] **Step 3: 实现 additive ORM 与 append-only ledger**

实现第 2.1、2.2 节精确字段和签名；事件数量始终为正数，方向由 `event_type` 决定；`reversal` 必须唯一引用原事件。

- [ ] **Step 4: 实现数据库约束和防篡改 trigger**

迁移必须包含：非负数量、状态枚举、项目/前置库关系、SN 唯一性、幂等唯一键、aggregate version 唯一键、reversal 唯一键，以及阻止 UPDATE/DELETE `maintenance_site_stock_event` 的 trigger。

- [ ] **Step 5: 迁移测试**

```python
def test_revision_descends_from_d9_and_creates_one_head():
    assert revision.down_revision == "d9f1a3c7e5b2"
    assert revision.revision == "a1d4e7f9c2b6"
```

另加两项数据库断言：升级后历史 shipment/issue 行不会自动生成 receipt、stock event 或 exemption；对 event 执行 SQL `UPDATE` 和 `DELETE` 均由 append-only trigger 拒绝且原行仍存在。

- [ ] **Step 6: 验证 Kernel**

```bash
uv run --extra dev pytest -q \
  tests/test_maintenance_site_stock_logic.py \
  tests/test_maintenance_site_stock_migration.py
uv run alembic heads
uv run alembic check
```

Expected: 单 head `a1d4e7f9c2b6`，全部测试通过。

- [ ] **Step 7: 提交并接受双重 review**

```bash
git add backend/app backend/alembic/versions/a1d4e7f9c2b6_maintenance_site_stock_closure.py backend/tests backend/.env.example .env.example docker-compose.yml
git commit -m "feat(maintenance): add append-only front-stock kernel"
```

必须分别通过 spec compliance review 和 migration/concurrency code review，再合到 integration。

### Task 3: 项目化购物车与实名审批（Lane A，与 Tasks 4–6 并行）

**Files:**
- Modify: `backend/app/services/replenishment.py`
- Modify: `backend/app/api/replenishment.py`
- Modify: `backend/tests/test_replenishment_beta.py`
- Modify: `frontend/src/api/replenishment.ts`
- Modify: `frontend/src/pages/ReplenishmentBetaPage.tsx`
- Modify: `frontend/src/pages/ReplenishmentBetaPage.css`
- Modify: `frontend/src/pages/__tests__/ReplenishmentBetaPage.test.tsx`

**Interfaces:**
- Consumes: `MaintenanceFrontWarehouse`、项目 scope、只读 `list_project_site_stock`
- Produces: project-bound immutable application、evidence snapshot、line risk flags、human review result

- [ ] **Step 1: 写失败的后端测试**

增加六个具名用例并使用真实 API 响应断言：无项目权限或非 active 前置库创建返回 403/409；提交快照同时包含使用、成本、销售和跨项目余量；风险码不自动驳回；硬拦截码阻止提交；提交人自审返回 403；批准前后 `maintenance_site_stock_event` 行数恒定。

- [ ] **Step 2: 扩展 create/update/submit/review**

创建时必填 `project_id`、`front_warehouse_id`、`required_at`、`request_note`；服务端验证项目 scope 和前置库归属。提交时冻结 180 天 confirmed usage、现有半年采购/销售、池政策、其他项目前置库存快照，并生成第 1.2 节 reason codes。

- [ ] **Step 3: 保持审批边界**

逐行 `approved/rejected`；风险行可由不同于提交人的实名 reviewer 带理由通过；硬拦截行不能提交。通过后状态仍表示内部供货许可，WBDD subset export 不写库存。

- [ ] **Step 4: 前端先写失败测试**

增加三个 React Testing Library 用例：未选择项目和前置库时“创建申请”disabled；mock evidence 返回后可见频次、成本依据、跨项目余量和风险原因；批准状态只显示“待执行供货”，DOM 中不存在“已发货”或“已消耗”。

- [ ] **Step 5: 实现购物车证据与审批 UI**

申请人页面展示项目、前置库、总估算金额、逐行证据、版本和审核原因；审核能力账号展示逐行通过/驳回控件。普通业务页隐藏 digest 等内部字段，放入“查看数据依据”。

- [ ] **Step 6: 验证并提交**

```bash
cd backend && uv run --extra dev pytest -q tests/test_replenishment_beta.py
cd ../frontend && npm run test -- ReplenishmentBetaPage.test.tsx
git add backend/app/services/replenishment.py backend/app/api/replenishment.py backend/tests/test_replenishment_beta.py frontend/src/api/replenishment.ts frontend/src/pages/ReplenishmentBetaPage.tsx frontend/src/pages/ReplenishmentBetaPage.css frontend/src/pages/__tests__/ReplenishmentBetaPage.test.tsx
git commit -m "feat(replenishment): bind approval cart to project front stock"
```

### Task 4: 现场收货与前置库查询（Lane B，与 Tasks 3/5/6 并行）

**Files:**
- Implement: `backend/app/api/maintenance_site_stock.py`
- Create: `backend/app/services/maintenance_site_receipts.py`
- Create: `backend/tests/test_maintenance_site_stock_api.py`
- Create: `frontend/src/api/maintenanceSiteStock.ts`
- Create: `frontend/src/components/maintenance/FrontStockPanel.tsx`
- Create: `frontend/src/components/maintenance/__tests__/FrontStockPanel.test.tsx`

**Interfaces:**
- Consumes: frozen ledger API、warehouse delivery bridge、project scope
- Produces: receipt command、front-stock directory、reconciliation report、project component

- [ ] **Step 1: 写失败的收货 API 测试**

增加五个具名 API 用例：只有 shipment 时余额为 0；发货 10 实收 9 后余额为 9 且待收为 1；同 key 重放 event 数不变；错误项目/PN/SN/mapping version 全部原子拒绝；write flag 或项目 allowlist 未命中时返回 404/403 且零事件。

- [ ] **Step 2: 实现原生收货命令**

前端只提交稳定发货行、数量、mapping version、key、reason；项目、PN、SN、可收上限由服务端从 delivery source 推导。部分收货允许，多收拒绝。

- [ ] **Step 3: 实现前置库查询与库龄**

聚合事件并返回第 2.3 节字段；`oldest_receipt_date` 来自未耗尽批次最早 receipt event；成本缺失返回 null 与明确 evidence state。

- [ ] **Step 4: 实现 reconciliation**

输出：发货、实收、差异、领用、好件退回、余额、负库存、缺稳定关系、重复事件、成本缺口计数；查询不反写任何业务事实。

- [ ] **Step 5: 写并实现前端组件**

增加三个组件用例：五个数量/库龄字段分别显示；收货请求只携带稳定 delivery line、mapping version 和 UUID key；成本证据缺失时显示“暂无可靠成本”，不渲染 `¥0.00`。

- [ ] **Step 6: 验证并提交**

```bash
cd backend && uv run --extra dev pytest -q tests/test_maintenance_site_stock_api.py
cd ../frontend && npm run test -- FrontStockPanel.test.tsx
git add backend/app/api/maintenance_site_stock.py backend/app/services/maintenance_site_receipts.py backend/tests/test_maintenance_site_stock_api.py frontend/src/api/maintenanceSiteStock.ts frontend/src/components/maintenance/FrontStockPanel.tsx frontend/src/components/maintenance/__tests__/FrontStockPanel.test.tsx
git commit -m "feat(maintenance): confirm site receipts and expose front stock"
```

### Task 5: 领用单核销真实消耗（Lane C，与 Tasks 3/4/6 并行）

**Files:**
- Create: `backend/app/services/maintenance_site_consumption.py`
- Modify: `backend/app/services/maintenance_project_operations.py`
- Modify: `backend/app/api/maintenance_project_operations.py`
- Modify: `backend/tests/test_site_issue_v2_api.py`
- Modify: `frontend/src/api/maintenanceOperations.ts`
- Modify: `frontend/src/components/maintenance/SiteIssueWorkflowPanel.tsx`
- Modify: `frontend/src/components/maintenance/__tests__/SiteIssueWorkflowPanel.test.tsx`

**Interfaces:**
- Consumes: `append_site_stock_command`、existing cost freeze、existing return event
- Produces: consumption events in same transaction、`inventory_effect=site_stock_decrease`

- [ ] **Step 1: 写失败的事务测试**

增加六个具名事务用例：未收货不可消耗；实收 9 领用 3 后余额 6；人为令返还义务写入失败时成本/issue/stock event 全回滚；两个并发 issue 合计不能超过余额；void/correction 只新增 reversal 且旧事件 hash 不变；flag off 时响应仍为 `inventory_effect=none` 且零 stock event。

- [ ] **Step 2: 改造确认事务**

命名试点项目且 write flag 开启时，确认顺序固定：锁 delivery rows → 查已收可用量 → 冻结成本 → 追加 consumption events → 生成返还义务 event → commit。任一步失败则整单回滚。

- [ ] **Step 3: 区分新件和换下旧件**

API 增加 `return_expected` 和 `removed_serial_number`。`return_expected=false` 不生成坏件义务；为 true 时 V1 使用同 part/PN、独立旧 SN。序列化品类缺旧 SN 时阻断确认。

- [ ] **Step 4: 实现纠错和作废**

已确认领用的 correction/void 只追加与旧 consumption event 一一对应的 reversal；成本和返还义务沿现有版本化链同步纠正，不删除旧记录。

- [ ] **Step 5: 更新前端语义**

移除“全过程不修改前置库库存”；改为明确显示“确认后前置库存减少并形成项目实际消耗”。表单增加是否换件、换下旧件 SN；确认结果展示 event IDs 和剩余量。

- [ ] **Step 6: 验证并提交**

```bash
cd backend && uv run --extra dev pytest -q tests/test_site_issue_v2_api.py
cd ../frontend && npm run test -- SiteIssueWorkflowPanel.test.tsx
git add backend/app/services/maintenance_site_consumption.py backend/app/services/maintenance_project_operations.py backend/app/api/maintenance_project_operations.py backend/tests/test_site_issue_v2_api.py frontend/src/api/maintenanceOperations.ts frontend/src/components/maintenance/SiteIssueWorkflowPanel.tsx frontend/src/components/maintenance/__tests__/SiteIssueWorkflowPanel.test.tsx
git commit -m "feat(maintenance): reconcile site issues against received stock"
```

### Task 6: 好件退回、项目免返和坏件 SN（Lane D，与 Tasks 3–5 并行）

**Files:**
- Implement: `backend/app/api/maintenance_good_returns.py`
- Create: `backend/app/services/maintenance_good_returns.py`
- Create: `backend/app/services/maintenance_return_policies.py`
- Modify: `backend/app/services/maintenance_bad_returns.py`
- Modify: `backend/app/api/maintenance_bad_returns.py`
- Create: `backend/tests/test_maintenance_good_returns_api.py`
- Modify: `backend/tests/test_maintenance_bad_returns_logic.py`
- Modify: `backend/tests/test_maintenance_bad_returns_api.py`
- Create: `frontend/src/api/maintenanceGoodReturns.ts`
- Create: `frontend/src/components/maintenance/GoodReturnPanel.tsx`
- Create: `frontend/src/components/maintenance/__tests__/GoodReturnPanel.test.tsx`
- Modify: `frontend/src/components/maintenance/BadReturnPanel.tsx`
- Modify: `frontend/src/components/maintenance/__tests__/BadReturnPanel.test.tsx`

**Interfaces:**
- Consumes: frozen good-return ORM、ledger API、warehouse receipt facts
- Produces: good-return state machine、project return policy matching、SN-complete obligation/return views

- [ ] **Step 1: 写失败的好件测试**

增加五个具名好件用例：draft/submit 前后余额相同；dispatch 只扣一次；不匹配或 pending receipt 不能 warehouse-confirm；全流程 obligation 行数不增加；好件 dispatch 与 site issue 并发竞争同一余额时最多一方取得超出部分。

- [ ] **Step 2: 实现好件状态机**

`draft/submitted` 无库存影响；`dispatch` 追加 `good_return_dispatched`；`warehouse_confirm` 只在正式 receipt 稳定关联匹配后闭环且不二次扣减；发出后作废必须追加 reversal；仓库确认后只能走替代/纠错单。

- [ ] **Step 3: 写失败的政策测试**

增加五个具名政策用例：无政策硬盘为 required；本项目有效政策命中才 exempt；过期/他项目政策均不命中；未用新硬盘可以提交 good return；旧件 SN 在 issue、obligation、bad-return line 和仓库视图完全一致。

- [ ] **Step 4: 移除全局硬盘免返新逻辑**

新义务规则：品类缺失 → `pending_category`；有效项目政策命中 → `exempt`；其他 → `required`。历史 `legacy-hard-drive-v1` 记录不在迁移中重算，通过显式人工复核另行修正。

- [ ] **Step 5: 实现前端好件/坏件分栏**

好件页展示草稿、在途、正式回库证据；坏件页展示应返、已返、项目免返、SN 和政策证据。禁止把 warehouse-confirmed 文案写成 legacy 库存已增加。

- [ ] **Step 6: 验证并提交**

```bash
cd backend && uv run --extra dev pytest -q \
  tests/test_maintenance_good_returns_api.py \
  tests/test_maintenance_bad_returns_logic.py \
  tests/test_maintenance_bad_returns_api.py
cd ../frontend && npm run test -- GoodReturnPanel.test.tsx BadReturnPanel.test.tsx
git add backend/app/api/maintenance_good_returns.py backend/app/services/maintenance_good_returns.py backend/app/services/maintenance_return_policies.py backend/app/services/maintenance_bad_returns.py backend/app/api/maintenance_bad_returns.py backend/tests/test_maintenance_good_returns_api.py backend/tests/test_maintenance_bad_returns_logic.py backend/tests/test_maintenance_bad_returns_api.py frontend/src/api/maintenanceGoodReturns.ts frontend/src/components/maintenance/GoodReturnPanel.tsx frontend/src/components/maintenance/__tests__/GoodReturnPanel.test.tsx frontend/src/components/maintenance/BadReturnPanel.tsx frontend/src/components/maintenance/__tests__/BadReturnPanel.test.tsx
git commit -m "feat(maintenance): close good and bad return evidence chains"
```

### Task 7: 串行集成、路由和项目工作区（Integration Owner）

**Files:**
- Modify: `backend/app/main.py`
- Modify: `backend/app/permissions.py`
- Modify: `backend/app/beta_access.py`（仅在准入 helper 需要时）
- Modify: `backend/tests/test_maintenance_permission_structure.py`
- Modify: `backend/tests/test_maintenance_beta_gate.py`
- Modify: `backend/tests/test_beta_account_whitelist.py`
- Modify: `frontend/src/pages/maintenance/MaintenanceProjectWorkspacePage.tsx`
- Modify: `frontend/src/pages/maintenance/__tests__/MaintenanceProjectWorkspacePage.test.tsx`
- Modify: `frontend/src/components/maintenance/maintenancePermissions.ts`
- Modify: `.ai/CHANGELOG.md`

**Interfaces:**
- Consumes: Tasks 2–6 reviewed commits
- Produces: single integrated branch with mounted routes/components and no ownership conflicts

- [ ] **Step 1: 按依赖顺序合并**

```bash
git merge --no-ff codex/maint-spares-kernel
git merge --no-ff codex/maint-spares-approval
git merge --no-ff codex/maint-spares-receipt
git merge --no-ff codex/maint-spares-consumption
git merge --no-ff codex/maint-spares-return
```

每次 merge 后运行 `git diff --check` 和对应快速测试；冲突只由 Integration Owner 解决。

- [ ] **Step 2: 注册 routers 和权限**

新增最小动作权限：

```text
action_maintenance_site_receipt_manage
action_maintenance_good_return_manage
action_maintenance_return_policy_manage
```

它们均依赖 `page_maintenance`、`page_maintenance_beta`；收货/好件退回不依赖成本可见，成本金额按现有数据权限遮罩。

- [ ] **Step 3: 挂载项目页面**

项目工作区增加“前置库存”“现场领用”“好件退回”“坏件返还”四个任务区；组件各自请求 API，避免继续扩大 workspace 聚合接口。技术 digest、内部 event ID 放入“查看数据依据”。

- [ ] **Step 4: 写集成页面测试**

增加三个工作区集成用例：同一 project ID 下四个 panel 都挂载；逐一撤掉 action permission 后对应写按钮不可见；read flag off 时旧合同/费用/回款和原领用板块仍正常显示。

- [ ] **Step 5: 更新变更留痕**

`.ai/CHANGELOG.md` 记录 before、after、业务原因、迁移 revision、feature flags、测试结果和最终 commit SHA；不得把 CI 绿写成生产完成。

### Task 8: Golden E2E、守恒和安全审计（Validation Agents，可并行审查，修复串行）

**Files:**
- Create: `backend/tests/test_maintenance_spares_golden_e2e.py`
- Create: `backend/tests/fixtures/maintenance_spares_golden.json`
- Create: `docs/releases/maintenance-spares-pilot-acceptance.md`
- Modify only if findings require: files owned in Tasks 2–7

**Interfaces:**
- Consumes: integrated branch
- Produces: independently reviewable E2E evidence and zero-open-P0/P1 report

- [ ] **Step 1: 固化 golden 场景**

```text
发货 10 → 实收 9/差异 1 → 领用 3 → 可用 6
→ 未用好件发出退回 2 → 可用 4、消耗仍为 3
→ 正式 receipt 关联 2 → 好件退回闭环
→ 换下坏件形成独立义务 → 项目政策命中或应返 → 仓库确认
```

- [ ] **Step 2: 增加不变量测试**

覆盖重复 key、不同 payload 重放、并发超领、跨项目 403/404、SN 重复、无政策硬盘应返、未用硬盘好件退回、成本只计一次、flag off 兼容、事件不可改删。

- [ ] **Step 3: 三个独立 reviewer 并行审查**

```text
Reviewer 1：业务状态机和需求符合性
Reviewer 2：数据库迁移、幂等、事务和并发
Reviewer 3：RBAC、跨项目隔离、敏感成本遮罩和前端可访问性
```

每位 reviewer 必须输出 Critical/Important/Minor、精确文件行和复现方式。所有 Critical/Important 由单一 Fix Agent 一次修复，再做 scoped re-review。

- [ ] **Step 4: 聚焦验证**

```bash
cd backend
uv run --extra dev pytest -q \
  tests/test_maintenance_site_stock_logic.py \
  tests/test_maintenance_site_stock_api.py \
  tests/test_site_issue_v2_api.py \
  tests/test_maintenance_good_returns_api.py \
  tests/test_maintenance_bad_returns_logic.py \
  tests/test_maintenance_bad_returns_api.py \
  tests/test_replenishment_beta.py \
  tests/test_maintenance_spares_golden_e2e.py \
  tests/test_maintenance_permission_structure.py \
  tests/test_maintenance_beta_gate.py \
  tests/test_beta_account_whitelist.py
```

### Task 9: 全量 CI、本地生产副本演练与发布控制（Integration Owner）

**Files:**
- Create: `.deploy/maintenance_spares_manifest.py`
- Create: `.deploy/maintenance_spares_build.sh`
- Create: `.deploy/maintenance_spares_rehearse.sh`
- Create: `.deploy/maintenance_spares_release.sh`
- Create: `docs/releases/maintenance-spares-runbook.md`
- Create: `backend/tests/test_maintenance_spares_release_controls.py`

**Interfaces:**
- Consumes: exact merged main SHA、migration `d9 → a1`、named project/account allowlist
- Produces: fail-closed build/rehearse/release/contain/observe workflow

- [ ] **Step 1: 复制控制模式而非旧版本常量**

复用 v1.21 的 exact-SHA archive、manifest 签名、isolated restore、canary、observe、contain 思路；新脚本必须明确 `DB_FROM=d9f1a3c7e5b2`、`DB_TO=a1d4e7f9c2b6`，不得引用 v122/f9 草案。

- [ ] **Step 2: 全量测试**

```bash
cd backend
uv run --extra dev pytest -q
uv run alembic heads
uv run alembic upgrade head
uv run alembic check
cd ../frontend
npm ci
npm run test
npm run build
npm run audit:prod
```

- [ ] **Step 3: 生产只读样例契约核对**

通过 SSH 在生产端只读查询 schema、行数、状态分布，并复制最小样例到隔离目录；不得查询或输出密码/token，不得在生产执行 UPDATE/DELETE/TRUNCATE/DDL。样例核对必须确认 shipment 的 `confirmed` 不是现场签收。

- [ ] **Step 4: 生产副本迁移和旧镜像兼容演练**

在隔离 PostgreSQL/网络上完成：`d9 → a1`、新镜像 smoke、旧镜像在新 schema 上稳定接口 smoke、并发锁采样、restore drill。任何失败停止。

- [ ] **Step 5: GitHub merge gates**

要求 exact SHA 上后端/前端 CI 绿、两名独立 reviewer 的 P0/P1=0、PR scope/title 与实际 diff 一致、Alembic 单 head。PR #245 未合并时，本闭环 PR 只能保持 stacked 状态，不能越过基线直接进生产。

### Task 10: 备份、生产灰度和观察（仅 Root/Integration Owner）

**Files:**
- Runtime evidence only; do not modify source during deployment

**Interfaces:**
- Consumes: signed artifact、exact merged SHA、approved manifest、named canary accounts/project
- Produces: verified backup、deployment record、canary results、observation evidence

- [ ] **Step 1: 发布前全量备份**

至少备份：PostgreSQL custom-format dump、全局对象/角色清单、用户上传文件、生产 compose/env 的加密受控副本、当前镜像 digest、当前部署 SHA。为每个制品生成 SHA-256；日志只记录路径、大小和 digest，不打印密钥。

- [ ] **Step 2: 隔离恢复验收**

恢复到全新隔离实例，执行 `pg_restore --list`、关键表行数/约束/foreign key 核对、旧应用只读 smoke。恢复失败不得部署；备份文件不得在观察期结束前删除。

- [ ] **Step 3: 全 flag=false 部署**

部署 exact merged artifact，确认 DB head、image digest、health、旧功能真实账号 smoke。此时业务行为不得变化。

- [ ] **Step 4: shadow**

仅打开 `maintenance_front_stock_shadow_enabled`，write/read 保持 false；运行命名项目 reconciliation，与真实出库单、独立盘点和人员确认三方核对，守恒差异必须为 0。

- [ ] **Step 5: write canary**

仅对白名单项目和实名现场/维保/仓库账号开启 write，read 仍 false；走完 golden E2E，旧系统继续作为主读口径并双录核对。

- [ ] **Step 6: read canary**

事件级对账全通过后，只对同一项目开启 read；验证前置库存、真实消耗、好件在途、坏件返还和成本金额。

- [ ] **Step 7: 观察与次日业务对账**

执行 0/5/15/30 分钟技术观察，并在下一工作日完成业务对账。任一停止条件触发：关 read/write/shadow，总闸保持可关闭，保存审计证据，应用按已演练方案回切，数据库不 downgrade。

停止条件：负库存、重复成本/事件、稳定关系缺失仍写入、守恒差异、未正式入库即恢复可用、跨项目越权、免返无政策证据、5xx/容器重启/DB 长事务异常。

## 5. Agent 启动指令

协调 Claude Code 会话必须使用用户配置中的 `fable` alias；该 alias 当前映射到 `deepseek-v4-flash[1M]`。首条执行指令必须包含：

```text
你必须显式使用多开智能体，不得单线程完成全部实现。
先由一个 Schema/Kernel Agent 串行完成 Task 2 并通过双 review；然后同时启动至少四个独立 Agent，分别执行 Lane A/B/C/D。
每个 Agent 必须使用独立 git worktree、独立分支和严格文件所有权；不得共享脏工作树，不得直接接触生产。
四条 Lane 完成后，由单一 Integration Agent 按计划顺序合并；再并行启动三个只读 Reviewer 做业务、数据库并发、安全审查。
实现遵循 TDD：先见到精确失败，再写最小实现；每个 Lane 提交前运行自己的测试并写 report。
禁止删除生产数据，禁止直接修改生产库存，禁止跳过备份/恢复/真实账号验收。
```

## 6. Definition of Done

- [ ] 购物车绑定稳定项目/前置库，提交版本及证据不可变，实名逐行审批完整。
- [ ] 审批通过零库存影响，文案不暗示已发货/已采购。
- [ ] 发货 10、实收 9、领用 3、好件退回 2 后前置库可用严格为 4。
- [ ] 真实消耗严格为已确认领用 3，成本只冻结和累计一次。
- [ ] 坏件返还义务与新件库存分账，SN 从领用到返还可追踪。
- [ ] 普通硬盘默认应返，项目有效政策才能免返，未用硬盘仍可好件退回。
- [ ] 返还提交不恢复公司库存；正式 receipt 稳定关联后才显示闭环。
- [ ] 幂等、并发、纠错、权限、成本遮罩、flag-off 兼容测试全部通过。
- [ ] Alembic 单 head `a1d4e7f9c2b6`，production-copy rehearsal 和 isolated restore 通过。
- [ ] 后端全量 pytest、前端 test/build/audit、GitHub CI 全绿。
- [ ] 两名独立代码 reviewer 与三域审查均无 P0/P1。
- [ ] 全量备份、checksum、隔离恢复、exact SHA 和 named canary 证据齐全。
- [ ] 0/5/15/30 分钟技术观察和次一工作日业务对账通过后，状态才可标记为“可灰度/可生产”。

## 7. 明确后置

- LangGraph/LLM 自动审核与自动驳回。
- 自动生成具有法律效力的正式采购订单。
- 补库批准到采购、到货、WBDD 的自动桥接。
- 销售归属模型和销售本人项目大屏。
- 报销/差旅/外包费用正式导入（等待真实字段合同）。
- 坏件维修、报废、变卖、虚拟供应商、销售订单和贡献毛利。
- 五模块老板驾驶舱、测试工位、拆改配、源头 case、发票/退货/退款。
- 全量氚云同步、多前置库、多公司仓事务库存切换。

这些事项不得塞进首版闭环 PR，也不得用演示数据伪装已经完成。
