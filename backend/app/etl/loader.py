"""幂等入库：维度 upsert（字段优先级）+ 事实去重 upsert + 库存求和（§6.4/§7.5）。

调用方（pipeline）负责事务边界与 batch。本模块所有写操作复用传入 session。

主数据重定向（整改 P3）——导入时商品身份解析按三层确定，防止治理成果被导入冲掉：
1. 别名重定向：pn_raw 命中 status='active' 且指向其它型号的 part_alias
   （人工审核接受的别名/合并产生的映射）→ 直接用别名的 part_id，不再重复建档；
2. 合并重定向：pn_std 命中 status='merged' 的墓碑行 → 沿 merged_into_id 链取目标
   （合并服务做路径压缩，链长通常 ≤1，循环上限防御坏数据）；
3. 其余 → 照常按 pn_std upsert 建档。
明细行 pn_std/pn_raw 一律保留 cleaner 归一原文（不改写为目标 pn）——这是
追溯与合并回滚归属判定的前提；商品身份只体现在 part_id。
"""
import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import case, delete, func, or_, select, true
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.etl import expense_void, mapping
from app.etl.transform import SOFT_ERROR_TYPES, TransformResult
from app.models.dimensions import DimCustomer, DimPart, DimSupplier, PartAlias
from app.models.inventory import Inventory
from app.models.maintenance import (
    FMaintenanceLine,
    FMaintenanceOrder,
    FProjectExpense,
    MaintenanceContractWorkbookState,
)
from app.models.maintenance_project import MaintenanceProject
from app.models.maintenance_project_operations import (
    MaintenanceProjectWorkbookState,
)
from app.models.maintenance_source_assignment import MaintenanceSourceOrderAssignment
from app.models.purchase import FPurchaseLine, FPurchaseOrder
from app.models.sales import FSalesLine, FSalesOrder
from app.models.system import SysAuditLog
from app.services import maintenance_cost_invalidation
from app.services import maintenance_project_operations as _project_ops

_log = logging.getLogger("loader")
_CHUNK = 1000  # 每批行数，控制单语句参数数 < PostgreSQL 65535 上限
_MERGE_CHAIN_LIMIT = 10
# 覆盖留痕（🥉/§3）：upsert/库存覆盖既有行时写 before/after，超量只记汇总防审计爆量。
_AUDIT_OVERWRITE_MAX = 2000
_AUDIT_IGNORE = {"import_batch_id"}   # 变化检测忽略（每次导入必变，非业务冲突）
# 采购/销售行的导入自有 flag（anomaly.line_flags 决策树根只产出这两个）；
# profit.recompute 会追加派生 flag（no_cost/neg_margin/excluded_*），重导时
# 被覆盖回导入子集——该 flap 不算语义变化（changed_keys 判定的 compare_subset）。
_ORDER_LINE_IMPORT_FLAGS = frozenset({"zero_price", "amount_mismatch"})


def _jsonable(v):
    """JSONB 安全化：Decimal→str、date/datetime→isoformat，其余原样（list[str]/int/None 均可）。"""
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, (date, datetime)):
        return v.isoformat()
    return v


def _audit_overwrites(session: Session, entity_type: str, table: str,
                      entries: list[tuple], operated_by: str | None, batch_id: int) -> None:
    """对被覆盖的行写 SysAuditLog（before/after）。entries: [(entity_id, before_dict, after_dict)]。"""
    if not entries:
        return
    reason = f"导入覆盖（batch {batch_id} · {table}）"
    if len(entries) > _AUDIT_OVERWRITE_MAX:
        session.add(SysAuditLog(
            entity_type=entity_type, entity_id=batch_id, action="overwrite",
            before_json=None,
            after_json={"overwritten_rows": len(entries),
                        "note": f"超过 {_AUDIT_OVERWRITE_MAX} 行，仅记总数"},
            reason=reason, operated_by=operated_by))
        return
    for eid, before, after in entries:
        session.add(SysAuditLog(
            entity_type=entity_type, entity_id=eid, action="overwrite",
            before_json=before, after_json=after, reason=reason, operated_by=operated_by))


class MergeChainError(Exception):
    """merged_into_id 链超限/成环（数据损坏），拒绝整批导入。"""


class ImportConcurrencyConflict(RuntimeError):
    """A concurrent ownership change invalidated the import lock envelope."""


class ImportIntegrityError(ValueError):
    """Imported facts cannot be projected without guessing or stale data."""


class WorkbookInvalidationConflictError(ImportConcurrencyConflict):
    """写后复核发现归属项目超出 upsert 前的预锁集合——fail closed，整批回滚。"""


@dataclass
class MaintenanceImportLockEnvelope:
    """WBDD 导入前预锁的自动归属目标；load 后只允许复用，不得补锁。"""

    target_project_ids: set[str]
    states: dict[str, MaintenanceProjectWorkbookState] = field(default_factory=dict)
    projects: dict[str, MaintenanceProject] = field(default_factory=dict)


def _probe_assigned_project_ids(session: Session,
                                source_order_ids) -> set[str]:
    """当前生效挂靠 probe（只读不加锁）：source_order_id → 稳定项目 ID 集合。"""
    ids = sorted({str(s) for s in source_order_ids if s})
    if not ids:
        return set()
    out: set[str] = set()
    for chunk in _chunks(ids):
        out.update(session.scalars(
            select(MaintenanceSourceOrderAssignment.project_id).where(
                MaintenanceSourceOrderAssignment.source_order_id.in_(chunk),
                MaintenanceSourceOrderAssignment.is_active.is_(True),
            )
        ))
    return out


def _prelock_workbook_states(session: Session, source_order_ids) -> dict:
    """事实写入前：按解析到的 source order IDs probe 归属项目并排序预锁 state。

    全局锁序要求（K3 writer-side workbook revision invalidation）：workbook state
    必须先于任何 order/line 事实行锁获取；禁止在事实行锁之后再拿新 state 锁。
    """
    return _project_ops.lock_workbook_states(
        session,
        project_ids=_probe_assigned_project_ids(session, source_order_ids),
    )


def _bump_workbooks_for_changed_orders(
    session: Session,
    *,
    prelocked: dict,
    source_order_ids,
    changed_source_order_ids,
) -> list[str]:
    """写后复核 + 精确失效：语义变化的归属项目在同事务各 bump 一次。

    复核只读比对：若当前挂靠里出现预锁集合外的项目（导入期间被并发挂靠），
    此处绝不能新拿 state 锁（order/line 行锁已持有，反序即死锁面）——直接
    回滚整批 fail closed，调用方重试即可收敛。无业务字段变化（仅
    import_batch_id/时间戳刷新）的项目不 bump。
    """
    unexpected = _probe_assigned_project_ids(session, source_order_ids) - set(prelocked)
    if unexpected:
        raise WorkbookInvalidationConflictError(
            "导入期间出现预锁集合外的项目挂靠（并发变更），请重试："
            f"{sorted(unexpected)}"
        )
    bumped: list[str] = []
    for pid in sorted(_probe_assigned_project_ids(session, changed_source_order_ids)):
        state = prelocked.get(pid)
        if state is None:
            continue  # 不可能分支：changed ⊆ probe 范围，且上面已 fail closed
        _project_ops.bump_locked_workbook_revision(session, state=state)
        bumped.append(pid)
    return bumped


def _chunks(seq: list, n: int = _CHUNK):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def _alias_redirects(session: Session, rows: list[dict]) -> dict[str, int]:
    """pn_raw → part_id：已生效且指向「与本行归一结果不同型号」的别名。

    恒等别名（alias.pn_std == 行 pn_std）不构成重定向，照常走建档/enrich 路径。
    """
    raw_to_std = {r["pn_raw"]: r["pn_std"] for r in rows if r.get("pn_raw")}
    if not raw_to_std:
        return {}
    out: dict[str, int] = {}
    for chunk in _chunks(list(raw_to_std.keys())):
        for pn_raw, a_std, a_part in session.execute(
            select(PartAlias.pn_raw, PartAlias.pn_std, PartAlias.part_id).where(
                PartAlias.pn_raw.in_(chunk),
                PartAlias.status == "active",
                PartAlias.part_id.is_not(None),
            )
        ).all():
            if a_std != raw_to_std[pn_raw]:
                out[pn_raw] = a_part
    return out


def _resolve_merged(session: Session, ids: set[int]) -> dict[int, tuple[int, str]]:
    """id → (最终 part_id, 最终 pn_std)：沿 merged_into_id 链重定向到 active 行。"""
    out: dict[int, tuple[int, str]] = {}
    if not ids:
        return out
    info: dict[int, tuple[str, int | None, str]] = {}

    def _load(batch: set[int]) -> None:
        for chunk in _chunks(list(batch)):
            for pid, pn, status, into in session.execute(
                select(DimPart.id, DimPart.pn_std, DimPart.status, DimPart.merged_into_id)
                .where(DimPart.id.in_(chunk))
            ).all():
                info[pid] = (status, into, pn)

    _load(ids)
    for start in ids:
        cur = start
        for _ in range(_MERGE_CHAIN_LIMIT):
            status, into, pn = info[cur]
            if status != "merged" or into is None:
                out[start] = (cur, pn)
                break
            if into not in info:
                _load({into})
            cur = into
        else:
            raise MergeChainError(f"part {start} 的合并链超过 {_MERGE_CHAIN_LIMIT} 跳，疑似成环")
    return out


def _merge_part_attrs(rows: list[dict], is_sales: bool) -> dict[str, dict]:
    """同一 pn_std 聚合属性：描述/品牌/单位取首个非空，needs_review 取 OR，品类仅销售。"""
    out: dict[str, dict] = {}
    for r in rows:
        pn = r["pn_std"]
        a = out.setdefault(pn, {
            "pn_std": pn, "pn_raw_sample": r.get("pn_raw"),
            "description": None, "brand": None, "machine_or_part": None, "unit": None,
            "category_major": None, "category_minor": None, "needs_review": False,
        })
        for f in ("description", "brand", "machine_or_part", "unit"):
            if a[f] is None and r.get(f) is not None:
                a[f] = r[f]
        if is_sales:
            for f in ("category_major", "category_minor"):
                if a[f] is None and r.get(f) is not None:
                    a[f] = r[f]
        a["needs_review"] = a["needs_review"] or bool(r.get("needs_review"))
    return out


def _upsert_parts(session: Session, part_attrs: dict[str, dict], is_sales: bool) -> dict[str, int]:
    """upsert dim_part；返回 pn_std -> id（未做合并重定向的原始 id）。

    字段优先级（§7.5）：描述/品牌/单位 fill-if-empty；品类仅销售可写（COALESCE 新值优先）；
    needs_review 取 OR。占位品类已在 cleaner 置空，故采购/库存天然不写品类。
    merged 墓碑行不做属性 enrich（属性应落到合并目标上，由调用方重定向后处理）。
    """
    if not part_attrs:
        return {}

    def _respect_lock(field: str, normal):
        """采购人工维护过(locked_fields 含该字段)→ 一律保留人工值；否则走原优先级。
        "和氚云无 API、把服务器 PN 做成自治主数据"的地基：重导永不覆盖采购改过的字段。"""
        return case((DimPart.locked_fields.contains([field]), getattr(DimPart, field)),
                    else_=normal)

    for chunk in _chunks(list(part_attrs.values())):
        stmt = pg_insert(DimPart).values(chunk)
        # fill-if-empty：保留已有值，空则用新值（locked 字段则完全保留人工值）
        set_ = {
            "description": _respect_lock(
                "description", func.coalesce(DimPart.description, stmt.excluded.description)),
            "brand": _respect_lock(
                "brand", func.coalesce(DimPart.brand, stmt.excluded.brand)),
            "machine_or_part": _respect_lock(
                "machine_or_part",
                func.coalesce(DimPart.machine_or_part, stmt.excluded.machine_or_part)),
            "unit": _respect_lock("unit", func.coalesce(DimPart.unit, stmt.excluded.unit)),
            "pn_raw_sample": func.coalesce(DimPart.pn_raw_sample, stmt.excluded.pn_raw_sample),
            "needs_review": or_(DimPart.needs_review, stmt.excluded.needs_review),
        }
        if is_sales:
            # 销售可改写品类：新值非空优先（但采购锁定的品类不被覆盖）
            set_["category_major"] = _respect_lock(
                "category_major",
                func.coalesce(stmt.excluded.category_major, DimPart.category_major))
            set_["category_minor"] = _respect_lock(
                "category_minor",
                func.coalesce(stmt.excluded.category_minor, DimPart.category_minor))
        session.execute(stmt.on_conflict_do_update(
            index_elements=[DimPart.pn_std], set_=set_,
            where=(DimPart.status != "merged"),
        ))
    out: dict[str, int] = {}
    for chunk in _chunks(list(part_attrs.keys())):
        for pn, pid in session.execute(
            select(DimPart.pn_std, DimPart.id).where(DimPart.pn_std.in_(chunk))
        ).all():
            out[pn] = pid
    return out


def _resolve_line_parts(session: Session, rows: list[dict],
                        is_sales: bool) -> tuple[dict[str, tuple[int, str]], int]:
    """对一批明细/库存行做完整商品身份解析。

    返回 (pn_raw → (最终 part_id, 该 part 的 pn_std), 参与建档的型号数)。
    别名重定向的行不参与 dim_part 建档/enrich（防复活已治理的重复建档）。
    """
    redirect = _alias_redirects(session, rows)
    create_rows = [r for r in rows if r.get("pn_raw") not in redirect]
    part_attrs = _merge_part_attrs(create_rows, is_sales)
    pn_to_id = _upsert_parts(session, part_attrs, is_sales)

    resolved = _resolve_merged(session, set(pn_to_id.values()) | set(redirect.values()))
    out: dict[str, tuple[int, str]] = {}
    for r in rows:
        raw = r.get("pn_raw")
        pid = redirect.get(raw) if raw in redirect else pn_to_id.get(r["pn_std"])
        if pid is not None:
            out[raw] = resolved[pid]
    return out, len(part_attrs)


def _upsert_aliases(session: Session, rows: list[dict],
                    resolution: dict[str, tuple[int, str]]) -> None:
    """别名落库：pn_raw → (归属 part_id, 该 part 的 pn_std)。

    status 与 needs_review 由同一处派生（待审=pending），防双真相源；
    已有别名行不改写（人工审核结果优先，on conflict do nothing）。
    """
    seen = {}
    for r in rows:
        raw = r.get("pn_raw")
        if not raw or raw not in resolution:
            continue
        pid, canonical_pn = resolution[raw]
        pending = bool(r.get("needs_review"))
        seen[raw] = {"pn_raw": raw, "pn_std": canonical_pn, "part_id": pid,
                     "source": "auto", "needs_review": pending,
                     "status": "pending" if pending else "active"}
    if not seen:
        return
    for chunk in _chunks(list(seen.values())):
        stmt = pg_insert(PartAlias).values(chunk)
        session.execute(stmt.on_conflict_do_nothing(index_elements=[PartAlias.pn_raw]))


def _upsert_named_dim(session: Session, model, rows: list[dict], extra_cols: list[str]) -> dict[str, int]:
    """供应商/客户按 name_raw upsert，缺失属性 fill-if-empty。返回 name_raw -> id。"""
    dedup = {}
    for r in rows:
        if r.get("name_raw"):
            dedup[r["name_raw"]] = r
    if not dedup:
        return {}
    for chunk in _chunks(list(dedup.values())):
        stmt = pg_insert(model).values(chunk)
        set_ = {c: func.coalesce(getattr(model, c), getattr(stmt.excluded, c)) for c in extra_cols}
        session.execute(stmt.on_conflict_do_update(index_elements=[model.name_raw], set_=set_))
    out: dict[str, int] = {}
    for chunk in _chunks(list(dedup.keys())):
        for n, i in session.execute(select(model.name_raw, model.id).where(model.name_raw.in_(chunk))).all():
            out[n] = i
    return out


def _upsert_facts(session: Session, model, rows: list[dict], conflict_col,
                  update_cols: list[str] | None = None, audit: tuple | None = None,
                  track_changed: bool = False,
                  compare_subset: dict[str, frozenset] | None = None) -> dict:
    """事实表幂等写入。

    - update_cols=None(默认 skip)：ON CONFLICT DO NOTHING；返回 {inserted, skipped}。
    - update_cols=列名列表(upsert 修复模式)：ON CONFLICT DO UPDATE 这些字段；
      预先点一下已存在的键以区分新增/更新；返回 {inserted, updated}。
    - audit=(operated_by, batch_id)（仅 upsert 有意义）：覆盖既有行时把 before/after 写 SysAuditLog，
      只记业务字段确有变化的行（忽略 import_batch_id），供「后到覆盖先到」回溯（🥉/§3）。
    - track_changed=True：额外返回 changed_keys——本批发生**语义**写入的冲突键集合
      （新插入，或 upsert 后业务字段确有变化；import_batch_id 等每批必变的非业务
      字段单独变化不算）。供调用方做「事实变了才失效下游缓存」的精确判定。
    - compare_subset={列: 允许集}：该列在 changed_keys 判定中只比较导入自有的
      子集。anomaly_flags 是混合所有权列——导入写 import 期 flag，recompute
      会叠加派生 flag；全量比较会把「重导清掉派生 flag」误判为业务变化。
      audit 留痕仍按原值全量比较（既有语义不变）。
    """
    if not rows:
        return {"inserted": 0, "updated": 0, "skipped": 0, "changed_keys": set()}
    if update_cols is None:
        inserted = 0
        changed_keys: set = set()
        for chunk in _chunks(rows):
            stmt = pg_insert(model).values(chunk).on_conflict_do_nothing(index_elements=[conflict_col])
            got = session.execute(stmt.returning(conflict_col)).all()
            inserted += len(got)
            if track_changed:
                changed_keys.update(r[0] for r in got)
        return {"inserted": inserted, "updated": 0, "skipped": len(rows) - inserted,
                "changed_keys": changed_keys}

    def _cmp_value(col: str, value):
        """changed_keys 判定用的归一值：compare_subset 命中的列只留导入自有子集。"""
        if compare_subset and col in compare_subset:
            return sorted(v for v in (value or []) if v in compare_subset[col])
        return value

    # upsert 模式
    key_name = conflict_col.name
    keys = [r[key_name] for r in rows]
    before_by_key: dict = {}
    if audit is not None or track_changed:
        # 取既有行 id + 旧值（存原值，比较按数值/对象，避免 Decimal 标度误判）
        sel_cols = [conflict_col, model.id, *[getattr(model, c) for c in update_cols]]
        for chunk in _chunks(keys):
            for row in session.execute(select(*sel_cols).where(conflict_col.in_(chunk))).all():
                m = row._mapping
                before_by_key[m[key_name]] = (m["id"], {c: m[c] for c in update_cols})
        existing = len(before_by_key)
    else:
        existing = 0
        for chunk in _chunks(keys):
            existing += session.scalar(
                select(func.count()).select_from(model).where(conflict_col.in_(chunk))
            ) or 0
    for chunk in _chunks(rows):
        stmt = pg_insert(model).values(chunk)
        set_ = {c: getattr(stmt.excluded, c) for c in update_cols}
        session.execute(stmt.on_conflict_do_update(index_elements=[conflict_col], set_=set_))
    changed_keys = set()
    entries = []
    if audit is not None or track_changed:
        for r in rows:
            prev = before_by_key.get(r[key_name])
            if prev is None:
                changed_keys.add(r[key_name])      # 新插入：必然是语义写入
                continue
            eid, before = prev          # before: 原值（Decimal/date/...）
            after = {c: r.get(c) for c in update_cols}
            if audit is not None and any(
                _cmp_value(c, before.get(c)) != _cmp_value(c, after.get(c))
                for c in update_cols if c not in _AUDIT_IGNORE
            ):
                entries.append((eid, {c: _jsonable(v) for c, v in before.items()},
                                {c: _jsonable(v) for c, v in after.items()}))
            if track_changed and any(
                _cmp_value(c, before.get(c)) != _cmp_value(c, after.get(c))
                for c in update_cols if c not in _AUDIT_IGNORE
            ):
                changed_keys.add(r[key_name])
    if audit is not None and entries:
        op_by, b_id = audit
        _audit_overwrites(session, "import_overwrite", model.__tablename__, entries, op_by, b_id)
    return {"inserted": len(rows) - existing, "updated": existing, "skipped": 0,
            "changed_keys": changed_keys if track_changed else set()}


# 可更新字段(upsert 修复模式)：排除主键 raw_*_id 与利润派生字段(recompute 专属)
_PURCHASE_ORDER_UPD = ["order_no", "order_date", "purchaser", "supplier_id",
                        "linked_sales_order_no", "linked_maintenance_order_no",
                        "source_type", "source_type_raw",
                        "amount_ex_tax", "tax_rate", "is_tax_inclusive", "tax_amount",
                        "amount_inc_tax", "data_status", "import_batch_id"]
_PURCHASE_LINE_UPD = ["order_id", "line_no", "part_id", "pn_std", "pn_raw", "description",
                       "brand", "machine_or_part", "unit", "qty", "unit_price", "line_amount",
                       "recent_purchase_price", "anomaly_flags", "import_batch_id"]
_SALES_ORDER_UPD = ["order_no", "order_date", "salesperson", "customer_id",
                     "business_type", "warehouse", "amount_ex_tax", "tax_rate",
                     "data_status", "import_batch_id"]
_SALES_LINE_UPD = ["order_id", "line_no", "part_id", "pn_std", "pn_raw", "description",
                    "brand", "category_major", "category_minor", "machine_or_part", "unit",
                    "qty", "unit_price", "line_amount", "generic_product", "serial_numbers",
                    "anomaly_flags", "import_batch_id"]
# 维保：legacy 成本、双税成本及 reference_* provenance 均由
# maintenance_cost.recompute 独占回填；导入修复不得覆盖。
# 展示补全列（plan v1.3 §3）加入白名单：快照重传可刷新展示列，成本列继续排除。
_MAINT_ORDER_UPD = ["order_no", "order_date", "linked_sales_order_no", "project_raw",
                     "project_std", "customer_id", "end_customer", "demand_type",
                     "business_type", "salesperson", "warehouse", "maint_start", "maint_end",
                     "data_status", "import_batch_id",
                     *mapping.MAINTENANCE_HEAD_DISPLAY_FIELDS]
_MAINT_LINE_UPD = ["order_id", "line_no", "part_id", "pn_std", "pn_raw", "description",
                    "qty", "return_qty", "serial_numbers", "anomaly_flags", "import_batch_id",
                    *mapping.MAINTENANCE_LINE_DISPLAY_FIELDS]


def load(session: Session, result: TransformResult, batch_id: int, snapshot_date: date,
         mode: str = "skip", operated_by: str | None = None,
         audit_overwrites: bool = False,
         maintenance_lock_envelope: MaintenanceImportLockEnvelope | None = None) -> dict:
    if result.file_type == mapping.INVENTORY:
        return _load_inventory(session, result, batch_id, snapshot_date,
                               operated_by, audit_overwrites)
    if result.file_type == mapping.MAINTENANCE:
        return _load_maintenance(
            session,
            result,
            batch_id,
            mode,
            operated_by,
            audit_overwrites,
            maintenance_lock_envelope,
        )
    if result.file_type == mapping.EXPENSE:
        return _load_expense(session, result, batch_id, mode, operated_by, audit_overwrites)
    return _load_orders(session, result, batch_id, mode, operated_by, audit_overwrites)


def _wbdd_raw_ids_linked_to_sales(session: Session, sales_order_nos) -> list[str]:
    """XSDD 回退层（boss board #51）的桥：销售单号 → 挂靠它的 WBDD raw_order_id。"""
    nos = sorted({str(n) for n in sales_order_nos if n})
    if not nos:
        return []
    out: list[str] = []
    for chunk in _chunks(nos):
        out.extend(session.scalars(
            select(FMaintenanceOrder.raw_order_id).where(
                FMaintenanceOrder.linked_sales_order_no.in_(chunk),
            )
        ))
    return out


def _load_orders(session: Session, result: TransformResult, batch_id: int,
                 mode: str = "skip", operated_by: str | None = None,
                 audit_overwrites: bool = False) -> dict:
    upsert = (mode == "upsert")
    # 仅 upsert(覆盖)模式需留痕；skip 模式 ON CONFLICT DO NOTHING 不覆盖既有行
    audit = (operated_by, batch_id) if (upsert and audit_overwrites) else None
    is_sales = result.file_type == mapping.SALES
    # 0) K3 sales fallback：合同台账缺位项目的合同额证据来自挂靠 XSDD 的销售事实
    #    （boss board 回退层），销售头/行的业务字段实际变化使归属项目旧总表
    #    stale——与 WBDD 路径同一规则：先 probe+排序预锁 state，写后复核+bump。
    #    采购不写 V2 可见的项目合同/成本事实（成本只经 maintenance_cost.recompute
    #    派生，由 recompute 同事务 bump 覆盖），故此路径不挂 workbook 失效。
    linked_wbdd_raw_ids: list[str] = []
    prelocked_states: dict = {}
    previous_order_no_by_raw: dict[str, str] = {}
    previous_line_order_no_by_raw: dict[str, str] = {}
    if is_sales:
        incoming_order_raw_ids = sorted(result.orders)
        if incoming_order_raw_ids:
            previous_order_no_by_raw = dict(session.execute(
                select(FSalesOrder.raw_order_id, FSalesOrder.order_no).where(
                    FSalesOrder.raw_order_id.in_(incoming_order_raw_ids)
                )
            ).all())
        incoming_line_raw_ids = sorted(
            str(line["raw_line_id"])
            for line in result.lines
            if line.get("raw_line_id")
        )
        if incoming_line_raw_ids:
            previous_line_order_no_by_raw = dict(session.execute(
                select(FSalesLine.raw_line_id, FSalesOrder.order_no)
                .join(FSalesOrder, FSalesOrder.id == FSalesLine.order_id)
                .where(FSalesLine.raw_line_id.in_(incoming_line_raw_ids))
            ).all())
        # 同一 raw_order_id/raw_line_id 可在 upsert 中被修正到新销售单号。
        # 旧单号的回退证据会同时消失，故预锁集合必须覆盖 old+new；只锁新单号
        # 会漏掉仍引用旧单号的 WBDD 项目，使旧工作簿继续显示过期合同额。
        candidate_order_nos = {
            str(order["order_no"])
            for order in result.orders.values()
            if order.get("order_no")
        } | set(previous_order_no_by_raw.values()) | set(previous_line_order_no_by_raw.values())
        linked_wbdd_raw_ids = _wbdd_raw_ids_linked_to_sales(
            session, candidate_order_nos)
        prelocked_states = _prelock_workbook_states(session, linked_wbdd_raw_ids)
    # 1) 商品身份解析（别名/合并重定向 + 建档）+ alias
    resolution, new_parts = _resolve_line_parts(session, result.lines, is_sales)
    _upsert_aliases(session, result.lines, resolution)

    # 2) 供应商 / 客户
    orders = result.orders
    if is_sales:
        cust_rows = [{
            "name_raw": o["customer_name"], "name_normalized": o["customer_name"],
            "customer_type": o.get("customer_type"), "customer_source": o.get("customer_source"),
            "city": o.get("customer_city"),
        } for o in orders.values() if o.get("customer_name")]
        cust_id = _upsert_named_dim(session, DimCustomer, cust_rows,
                                    ["name_normalized", "customer_type", "customer_source", "city"])
    else:
        sup_rows = [{
            "name_raw": o["supplier_name_raw"], "name_normalized": o["supplier_name_normalized"],
            "supplier_code": o.get("supplier_code"), "supplier_type": o.get("supplier_type"),
            "source_channel": o.get("supplier_source_channel"),
        } for o in orders.values() if o.get("supplier_name_raw")]
        sup_id = _upsert_named_dim(session, DimSupplier, sup_rows,
                                   ["name_normalized", "supplier_code", "supplier_type",
                                    "source_channel"])

    # 3) 订单头
    order_model = FSalesOrder if is_sales else FPurchaseOrder
    order_rows = []
    for o in orders.values():
        base = {
            "raw_order_id": o["raw_order_id"], "order_no": o["order_no"],
            "order_date": o["order_date"], "amount_ex_tax": o["amount_ex_tax"],
            "tax_rate": o["tax_rate"], "data_status": o["data_status"],
            "import_batch_id": batch_id,
        }
        if is_sales:
            base.update({
                "salesperson": o.get("salesperson"),
                "customer_id": cust_id.get(o.get("customer_name")),
                "business_type": o.get("business_type"), "warehouse": o.get("warehouse"),
            })
        else:
            base.update({
                "purchaser": o.get("purchaser"),
                "supplier_id": sup_id.get(o.get("supplier_name_raw")),
                "source_type": o.get("source_type"), "source_type_raw": o.get("source_type_raw"),
                "linked_sales_order_no": o.get("linked_sales_order_no"),
                "linked_maintenance_order_no": o.get("linked_maintenance_order_no"),
                "is_tax_inclusive": o.get("is_tax_inclusive"),
                "tax_amount": o.get("tax_amount"),
                "amount_inc_tax": o.get("amount_inc_tax"),
            })
        order_rows.append(base)
    order_upd_cols = (_SALES_ORDER_UPD if is_sales else _PURCHASE_ORDER_UPD) if upsert else None
    order_stats = _upsert_facts(session, order_model, order_rows, order_model.raw_order_id,
                                order_upd_cols, audit=audit, track_changed=is_sales)
    # raw_order_id -> id（含已存在的）
    raw_ids = [o["raw_order_id"] for o in orders.values()]
    oid_map = dict(session.execute(
        select(order_model.raw_order_id, order_model.id).where(order_model.raw_order_id.in_(raw_ids))
    ).all())

    # 4) 明细行
    line_model = FSalesLine if is_sales else FPurchaseLine
    line_rows = []
    for ln in result.lines:
        base = {
            "raw_line_id": ln["raw_line_id"], "order_id": oid_map[ln["_order_raw_id"]],
            "line_no": ln["line_no"], "part_id": resolution[ln["pn_raw"]][0],
            "pn_std": ln["pn_std"], "pn_raw": ln["pn_raw"],
            "description": ln["description"], "brand": ln["brand"],
            "machine_or_part": ln["machine_or_part"], "unit": ln["unit"],
            "qty": ln["qty"], "unit_price": ln["unit_price"], "line_amount": ln["line_amount"],
            "anomaly_flags": ln["anomaly_flags"], "import_batch_id": batch_id,
        }
        if is_sales:
            base.update({
                "category_major": ln.get("category_major"), "category_minor": ln.get("category_minor"),
                "generic_product": ln.get("generic_product"), "serial_numbers": ln.get("serial_numbers"),
            })
        else:
            base["recent_purchase_price"] = ln["recent_purchase_price"]
        line_rows.append(base)
    line_upd_cols = (_SALES_LINE_UPD if is_sales else _PURCHASE_LINE_UPD) if upsert else None
    line_stats = _upsert_facts(session, line_model, line_rows, line_model.raw_line_id,
                               line_upd_cols, audit=audit, track_changed=is_sales,
                               compare_subset=(
                                   {"anomaly_flags": _ORDER_LINE_IMPORT_FLAGS}
                                   if is_sales else None
                               ))

    # 5) sales fallback 失效：变化单号 → 挂靠 WBDD → 归属项目各 bump 一次（写后复核
    #    发现预锁集合外项目则 fail closed 整批回滚，见 _bump_workbooks_for_changed_orders）。
    bumped_projects: list[str] = []
    if is_sales:
        line_order_raw = {ln["raw_line_id"]: ln["_order_raw_id"] for ln in result.lines}
        current_order_no_by_raw = dict(session.execute(
            select(FSalesOrder.raw_order_id, FSalesOrder.order_no).where(
                FSalesOrder.raw_order_id.in_(sorted(line_order_raw.values()))
            )
        ).all())
        current_line_order_no_by_raw = dict(session.execute(
            select(FSalesLine.raw_line_id, FSalesOrder.order_no)
            .join(FSalesOrder, FSalesOrder.id == FSalesLine.order_id)
            .where(FSalesLine.raw_line_id.in_(sorted(line_order_raw)))
        ).all()) if line_order_raw else {}
        changed_nos: set[str] = set()
        for raw in order_stats["changed_keys"]:
            if current := current_order_no_by_raw.get(raw):
                changed_nos.add(current)
            if previous := previous_order_no_by_raw.get(raw):
                changed_nos.add(previous)
        for raw_line_id in line_stats["changed_keys"]:
            # skip 模式可能保留既有 header，却插入一个来自“新单号”文件的
            # 新 line；此时 input order_no 不是数据库真正 parent。必须读回
            # current parent，不能按 incoming payload 推断。
            if current := current_line_order_no_by_raw.get(raw_line_id):
                changed_nos.add(current)
            if previous := previous_line_order_no_by_raw.get(raw_line_id):
                changed_nos.add(previous)
        bumped_projects = _bump_workbooks_for_changed_orders(
            session,
            prelocked=prelocked_states,
            source_order_ids=linked_wbdd_raw_ids,
            changed_source_order_ids=_wbdd_raw_ids_linked_to_sales(session, changed_nos),
        )

    return {
        "source_rows_total": result.rows_total,
        "fact_rows_inserted": line_stats["inserted"],
        "fact_rows_updated": line_stats["updated"],
        "fact_rows_skipped": line_stats["skipped"],
        "fact_rows_error": sum(1 for e in result.errors if e.error_type not in SOFT_ERROR_TYPES),
        "rows_inactive": result.rows_inactive,
        "orders_inserted": order_stats["inserted"],
        "orders_updated": order_stats["updated"],
        "import_mode": mode,
        "new_parts": new_parts,
        "workbook_projects_bumped": len(bumped_projects),
    }


def _load_maintenance(session: Session, result: TransformResult, batch_id: int,
                      mode: str = "skip", operated_by: str | None = None,
                      audit_overwrites: bool = False,
                      lock_envelope: MaintenanceImportLockEnvelope | None = None) -> dict:
    """维保出库（WBDD）入库：与订单路径同套路——商品身份解析 + 客户维度 + 头/行幂等 upsert。

    成本回填字段不在 upsert 白名单内（maintenance_cost.recompute 专属），重导不冲成本。

    工作簿失效（K3）：WBDD 头/行业务字段的实际 insert/update 会让所有当前归属项目
    的旧总表 stale——upsert 前先 probe+排序预锁 workbook state，写后复核并在
    同事务对语义变化的归属项目各 bump 一次 revision（仅 import_batch_id 刷新不 bump）。
    """
    upsert = (mode == "upsert")
    audit = (operated_by, batch_id) if (upsert and audit_overwrites) else None
    # 0) 归属项目预锁必须先于任何 order/line 行锁（全局锁序：state → 事实行）
    incoming_line_raw_ids = sorted(
        str(line["raw_line_id"])
        for line in result.lines
        if line.get("raw_line_id")
    )
    previous_line_order_raw: dict[str, str] = {}
    if incoming_line_raw_ids:
        previous_line_order_raw = dict(session.execute(
            select(FMaintenanceLine.raw_line_id, FMaintenanceOrder.raw_order_id)
            .join(FMaintenanceOrder, FMaintenanceOrder.id == FMaintenanceLine.order_id)
            .where(FMaintenanceLine.raw_line_id.in_(incoming_line_raw_ids))
        ).all())
    # raw_line_id 在 upsert 修复中可跨 WBDD 重挂；旧、新 parent 的项目都必须
    # 在事实行锁之前进入 state 预锁集合。
    source_order_ids = sorted(
        {o["raw_order_id"] for o in result.orders.values()}
        | set(previous_line_order_raw.values())
    )
    current_project_ids = _probe_assigned_project_ids(session, source_order_ids)
    target_project_ids = set(
        lock_envelope.target_project_ids if lock_envelope is not None else ()
    )
    prelocked_states = _project_ops.lock_workbook_states(
        session,
        project_ids=current_project_ids | target_project_ids,
    )
    if lock_envelope is not None:
        # 自动归属的既有目标也必须在事实行之前按 canonical 顺序锁住。
        # load 后的 apply 只能复用本信封，发现目标逃逸即整批 409 回滚。
        # PostgreSQL 不承诺一条 ``WHERE id IN (...) ORDER BY id FOR UPDATE``
        # 的实际加锁顺序；与 state 工具一致，逐 ID 获取项目锁，固定并发顺序。
        locked_projects = []
        for project_id in sorted(target_project_ids):
            project = session.scalar(
                select(MaintenanceProject)
                .where(MaintenanceProject.project_id == project_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if project is not None:
                locked_projects.append(project)
        lock_envelope.states = {
            project_id: prelocked_states[project_id]
            for project_id in target_project_ids
            if project_id in prelocked_states
        }
        lock_envelope.projects = {
            project.project_id: project for project in locked_projects
        }
    # 1) 商品身份解析（别名/合并重定向 + 建档，非销售口径：不写品类）+ alias
    resolution, new_parts = _resolve_line_parts(session, result.lines, is_sales=False)
    _upsert_aliases(session, result.lines, resolution)

    # 2) 客户维度（维保客户名实测混入联系人后缀，v1 原样入库，治理留二期）
    orders = result.orders
    cust_rows = [{
        "name_raw": o["customer_name"], "name_normalized": o["customer_name"],
        "customer_type": None, "customer_source": None, "city": None,
    } for o in orders.values() if o.get("customer_name")]
    cust_id = _upsert_named_dim(session, DimCustomer, cust_rows,
                                ["name_normalized", "customer_type", "customer_source", "city"])

    # 3) 维保单头
    order_rows = [{
        "raw_order_id": o["raw_order_id"], "order_no": o["order_no"],
        "order_date": o["order_date"],
        "linked_sales_order_no": o.get("linked_sales_order_no"),
        "project_raw": o.get("project_raw"), "project_std": o.get("project_std"),
        "customer_id": cust_id.get(o.get("customer_name")),
        "end_customer": o.get("end_customer"),
        "demand_type": o.get("demand_type"), "business_type": o.get("business_type"),
        "salesperson": o.get("salesperson"), "warehouse": o.get("warehouse"),
        "maint_start": o.get("maint_start"), "maint_end": o.get("maint_end"),
        "data_status": o["data_status"], "import_batch_id": batch_id,
        **{f: o.get(f) for f in mapping.MAINTENANCE_HEAD_DISPLAY_FIELDS},
    } for o in orders.values()]
    order_stats = _upsert_facts(session, FMaintenanceOrder, order_rows,
                                FMaintenanceOrder.raw_order_id,
                                _MAINT_ORDER_UPD if upsert else None, audit=audit,
                                track_changed=True)
    raw_ids = [o["raw_order_id"] for o in orders.values()]
    oid_map = dict(session.execute(
        select(FMaintenanceOrder.raw_order_id, FMaintenanceOrder.id)
        .where(FMaintenanceOrder.raw_order_id.in_(raw_ids))
    ).all())

    # 4) 出库明细行
    line_rows = [{
        "raw_line_id": ln["raw_line_id"], "order_id": oid_map[ln["_order_raw_id"]],
        "line_no": ln["line_no"], "part_id": resolution[ln["pn_raw"]][0],
        "pn_std": ln["pn_std"], "pn_raw": ln["pn_raw"],
        "description": ln["description"],
        "qty": ln["qty"], "return_qty": ln["return_qty"],
        "serial_numbers": ln["serial_numbers"],
        "anomaly_flags": ln["anomaly_flags"], "import_batch_id": batch_id,
        **{f: ln.get(f) for f in mapping.MAINTENANCE_LINE_DISPLAY_FIELDS},
    } for ln in result.lines]
    line_stats = _upsert_facts(session, FMaintenanceLine, line_rows,
                               FMaintenanceLine.raw_line_id,
                               _MAINT_LINE_UPD if upsert else None, audit=audit,
                               track_changed=True,
                               compare_subset={
                                   "anomaly_flags":
                                       maintenance_cost_invalidation.IMPORT_ANOMALY_FLAGS,
                               })

    # 5) 工作簿 revision 失效：写后复核（probe 外项目 → fail closed 整批回滚），
    #    再让「头或行确有业务字段变化」的单据归属项目各 bump 一次。
    current_line_order_raw = dict(session.execute(
        select(FMaintenanceLine.raw_line_id, FMaintenanceOrder.raw_order_id)
        .join(FMaintenanceOrder, FMaintenanceOrder.id == FMaintenanceLine.order_id)
        .where(FMaintenanceLine.raw_line_id.in_(incoming_line_raw_ids))
    ).all()) if incoming_line_raw_ids else {}
    changed_orders = set(order_stats["changed_keys"])
    for raw_line_id in line_stats["changed_keys"]:
        if current := current_line_order_raw.get(raw_line_id):
            changed_orders.add(current)
        if previous := previous_line_order_raw.get(raw_line_id):
            changed_orders.add(previous)
    bumped_projects = _bump_workbooks_for_changed_orders(
        session,
        prelocked=prelocked_states,
        source_order_ids=source_order_ids,
        changed_source_order_ids=changed_orders,
    )

    return {
        "source_rows_total": result.rows_total,
        "fact_rows_inserted": line_stats["inserted"],
        "fact_rows_updated": line_stats["updated"],
        "fact_rows_skipped": line_stats["skipped"],
        "fact_rows_error": sum(1 for e in result.errors if e.error_type not in SOFT_ERROR_TYPES),
        "rows_inactive": result.rows_inactive,
        "orders_inserted": order_stats["inserted"],
        "orders_updated": order_stats["updated"],
        "import_mode": mode,
        "new_parts": new_parts,
        # plan v1.3 M1-2/M1-3：无明细单头保留计数（样例 ≤50）与展示列坏值计数
        "headless_orders": len(result.headless_order_ids),
        "headless_order_ids_sample": result.headless_order_ids[:50],
        "rows_display_issue": result.rows_display_issue,
        # K3：本次语义写入导致 workbook revision 失效的项目数（仅归属项目各一次）
        "workbook_projects_bumped": len(bumped_projects),
    }


# 可更新字段（upsert 修复模式）：排除幂等主键 raw_line_id
_EXPENSE_UPD = ["bxd_no", "line_no", "data_status", "expense_date", "person",
                "expense_type", "fee_category", "reason", "linked_sales_order_no",
                "amount", "amount_ex_tax", "amount_inc_tax", "tax_basis",
                "tax_rate_used", "import_batch_id"]


def _invalidate_expense_snapshot_state(
    session: Session,
    *,
    contracts: set[str],
    batch_id: int,
    operated_by: str | None,
) -> None:
    """普通报销导入改变事实后，使旧“费用全量快照”声明立即失效。"""
    if not contracts:
        return
    rows = [
        {
            "contract_no": contract,
            "revision": 1,
            "expense_snapshot_complete": False,
            "last_import_batch_id": batch_id,
            "updated_by": operated_by,
        }
        for contract in sorted(contracts)
    ]
    stmt = pg_insert(MaintenanceContractWorkbookState).values(rows)
    session.execute(
        stmt.on_conflict_do_update(
            index_elements=[
                MaintenanceContractWorkbookState.contract_no,
            ],
            set_={
                "revision":
                    MaintenanceContractWorkbookState.revision + 1,
                "expense_snapshot_complete": False,
                "last_import_batch_id": batch_id,
                "updated_by": operated_by,
                "updated_at": func.now(),
            },
        ),
    )


def _load_expense(session: Session, result: TransformResult, batch_id: int,
                  mode: str = "skip", operated_by: str | None = None,
                  audit_overwrites: bool = False) -> dict:
    """报销明细入库：单表平铺，按 raw_line_id 幂等（§16.3/§17.4）。

    不建商品/客户维度（费用行无 PN）。项目归集以“发生日命中的唯一历史合同”为
    权威；没有正式合同证据时只允许沿稳定 WBDD 挂靠留下 unmapped 待治理事实，绝不
    猜合同。skip=增量；upsert=本文件覆盖所含 XSDD：文件里消失的旧行软作废，保留
    raw/FK/审计链，不再物理删除。

    raw、canonical attribution、项目工作簿 revision 在同一事务完成；因此导入成功
    后卡片/工作区下一次请求立即读取新成本，任一步失败则整批回滚。
    """
    from app.models.maintenance_project import (
        MaintenanceProject,
        MaintenanceProjectContract,
    )
    from app.models.maintenance_project_operations import (
        MaintenanceProjectExpenseAttribution,
    )
    from app.services.maintenance_expense_integrity import (
        ExpenseIntegrityError,
        OwnershipConflictError,
        expense_id_for,
        expense_ref_for,
        find_ownership_candidates,
        normalize_contract_no,
        sync_attribution_from_raw,
    )

    upsert = (mode == "upsert")
    audit = (operated_by, batch_id) if (upsert and audit_overwrites) else None
    incoming_by_id = {
        str(line["raw_line_id"]): line
        for line in result.lines
        if line.get("raw_line_id")
    }
    # 删除侧的输入与判定只认 expense_void：预演与执行共用同一份规则，不存在第二份实现
    void_inputs = expense_void.plan_inputs(result, mode=mode)
    contracts = set(void_inputs.contracts)
    incoming_ids = sorted(incoming_by_id)
    existing_scope: dict[str, FProjectExpense] = {}
    if incoming_ids:
        existing_scope.update({
            row.raw_line_id: row
            for row in session.scalars(
                select(FProjectExpense).where(
                    FProjectExpense.raw_line_id.in_(incoming_ids)
                )
            )
        })
    scope_contracts = expense_void.scope_contracts(void_inputs)
    if scope_contracts:
        existing_scope.update({
            row.raw_line_id: row
            for row in session.scalars(
                select(FProjectExpense).where(
                    FProjectExpense.linked_sales_order_no.in_(scope_contracts)
                )
            )
        })
    affected_ids = sorted(set(incoming_ids) | set(existing_scope))
    affected_expense_ids = [expense_id_for(raw_id) for raw_id in affected_ids]
    existing_attributions = list(session.scalars(
        select(MaintenanceProjectExpenseAttribution).where(
            or_(
                MaintenanceProjectExpenseAttribution.raw_expense_line_id.in_(affected_ids),
                MaintenanceProjectExpenseAttribution.expense_id.in_(affected_expense_ids),
            )
        )
    )) if affected_ids else []
    existing_attr_by_raw = {
        (row.raw_expense_line_id or row.expense_id.removeprefix("bxd:")): row
        for row in existing_attributions
    }

    # WBDD fallback is project evidence only, never a contract mapping.  Build it
    # once using the same normalized XSDD identity as historical contract lookup.
    fallback_projects: dict[str, set[str]] = {}
    for linked_no, project_id in session.execute(
        select(
            FMaintenanceOrder.linked_sales_order_no,
            MaintenanceSourceOrderAssignment.project_id,
        )
        .join(
            MaintenanceSourceOrderAssignment,
            MaintenanceSourceOrderAssignment.source_order_id
            == FMaintenanceOrder.raw_order_id,
        )
        .where(
            MaintenanceSourceOrderAssignment.is_active.is_(True),
            FMaintenanceOrder.linked_sales_order_no.is_not(None),
        )
    ):
        key = normalize_contract_no(linked_no)
        if key:
            fallback_projects.setdefault(key, set()).add(project_id)

    def _candidate_project_ids(linked_no, expense_date) -> set[str]:
        if not linked_no or expense_date is None:
            return set()
        candidates = find_ownership_candidates(
            session,
            linked_sales_order_no=linked_no,
            expense_date=expense_date,
        )
        if candidates:
            return {candidate.project_id for candidate in candidates}
        return set(fallback_projects.get(normalize_contract_no(linked_no), ()))

    # Probe every possible old/new owner before facts.  Later revalidation rejects
    # a project that appeared outside this envelope instead of taking a late state
    # lock (which would invert the global state→project→contract→attribution→raw order).
    lock_project_ids = {row.project_id for row in existing_attributions}
    for raw_id in affected_ids:
        incoming = incoming_by_id.get(raw_id)
        existing = existing_scope.get(raw_id)
        linked_no = (
            incoming.get("linked_sales_order_no")
            if incoming is not None
            else existing.linked_sales_order_no if existing is not None else None
        )
        expense_date = (
            incoming.get("expense_date")
            if incoming is not None
            else existing.expense_date if existing is not None else None
        )
        lock_project_ids.update(_candidate_project_ids(linked_no, expense_date))
    states = _project_ops.lock_workbook_states(
        session, project_ids=lock_project_ids
    )
    for project_id in sorted(lock_project_ids):
        project = session.scalar(
            select(MaintenanceProject)
            .where(MaintenanceProject.project_id == project_id)
            .with_for_update()
        )
        if project is None:
            raise WorkbookInvalidationConflictError(
                "报销导入期间项目已不存在，整批未写入，请重试"
            )
    if lock_project_ids:
        list(session.scalars(
            select(MaintenanceProjectContract)
            .where(MaintenanceProjectContract.project_id.in_(sorted(lock_project_ids)))
            .order_by(MaintenanceProjectContract.project_contract_id)
            .with_for_update()
        ))
    for raw_id in affected_ids:
        session.execute(select(func.pg_advisory_xact_lock(
            func.hashtextextended(f"maintenance-expense-row:{raw_id}", 0)
        )))
    if affected_expense_ids:
        list(session.scalars(
            select(MaintenanceProjectExpenseAttribution)
            .where(MaintenanceProjectExpenseAttribution.expense_id.in_(affected_expense_ids))
            .order_by(MaintenanceProjectExpenseAttribution.expense_id)
            .with_for_update()
            # These entities were loaded during the pre-lock ownership probe.
            # A concurrent ledger apply may have committed while we waited for
            # state/advisory locks; overwrite the identity-map snapshot with the
            # row version that is actually locked.
            .execution_options(populate_existing=True)
        ))
    if affected_ids:
        list(session.scalars(
            select(FProjectExpense)
            .where(FProjectExpense.raw_line_id.in_(affected_ids))
            .order_by(FProjectExpense.raw_line_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ))

    # Re-read after locks: an unlocked probe is never mutation authority.
    locked_existing = {
        row.raw_line_id: row
        for row in session.scalars(
            select(FProjectExpense).where(
                FProjectExpense.raw_line_id.in_(affected_ids)
            )
            .execution_options(populate_existing=True)
        )
    } if affected_ids else {}
    # 加锁重读之后、任何写入之前做判定；两道抑制（有行被排除 / 触及多合同）
    # 都在 expense_void.classify 里，这里只执行它给出的 void_ids。
    decision = expense_void.classify(locked_existing, void_inputs)
    replaced = len(locked_existing) if upsert else 0
    voided = 0
    void_protected = len(decision.protected_ids)
    if decision.suppressed_reason and contracts:
        # 抑制时不按合同扩宽 scope（不锁、不同步那些旧行），被保留的行数单独 COUNT：
        # 回执必须说清「本该作废多少行没作废」。
        # 与 expense_void.classify 同口径：NULL 状态也是作废候选（三值逻辑下裸
        # NOT IN 会把 NULL 排除，回执少算，Codex P2）。
        void_protected = session.scalar(
            select(func.count()).select_from(FProjectExpense).where(
                FProjectExpense.linked_sales_order_no.in_(sorted(contracts)),
                or_(FProjectExpense.data_status.is_(None),
                    FProjectExpense.data_status.not_in(sorted(expense_void.VOID_STATUSES))),
                FProjectExpense.raw_line_id.not_in(incoming_ids) if incoming_ids else true(),
            )
        ) or 0
    changed_raw_ids: set[str] = set()
    void_audits: list[tuple] = []
    for raw_id in decision.void_ids:
        row = locked_existing[raw_id]
        before = {c: _jsonable(getattr(row, c)) for c in _EXPENSE_UPD}
        row.data_status = "已作废"
        row.import_batch_id = batch_id
        changed_raw_ids.add(raw_id)
        voided += 1
        if audit:
            after = {c: _jsonable(getattr(row, c)) for c in _EXPENSE_UPD}
            void_audits.append((row.id, before, after))
    if audit and void_audits:
        _audit_overwrites(
            session,
            "import_overwrite",
            "f_project_expense(缺行作废)",
            void_audits,
            operated_by,
            batch_id,
        )
    rows = [{**ln, "import_batch_id": batch_id} for ln in result.lines]
    stats = _upsert_facts(session, FProjectExpense, rows, FProjectExpense.raw_line_id,
                          _EXPENSE_UPD if upsert else None, audit=audit,
                          track_changed=True)
    changed_raw_ids.update(stats["changed_keys"])

    session.flush()
    current_raw_by_id = {
        row.raw_line_id: row
        for row in session.scalars(
            select(FProjectExpense)
            .where(FProjectExpense.raw_line_id.in_(affected_ids))
            .order_by(FProjectExpense.raw_line_id)
            # _upsert_facts uses PostgreSQL Core INSERT .. ON CONFLICT and does
            # not synchronize ORM instances already present in the Session.
            # Without populate_existing, attribution sync mirrors the pre-upsert
            # amount/status and cards stay stale until a later import.
            .execution_options(populate_existing=True)
        )
    } if affected_ids else {}
    changed_project_ids: set[str] = set()
    attribution_counts: dict[str, dict[str, int]] = {}
    attributions_synced = 0
    attribution_duplicates_skipped = 0
    attribution_unowned_skipped = 0
    for raw_id in affected_ids:
        raw = current_raw_by_id.get(raw_id)
        if raw is None:
            raise WorkbookInvalidationConflictError(
                "报销导入行在应用期间消失，整批未写入，请重试"
            )
        candidates = (
            find_ownership_candidates(
                session,
                linked_sales_order_no=raw.linked_sales_order_no,
                expense_date=raw.expense_date,
            )
            if raw.expense_date is not None else ()
        )
        candidate_projects = {candidate.project_id for candidate in candidates}
        current_attr = session.get(
            MaintenanceProjectExpenseAttribution, expense_id_for(raw_id)
        )
        target_project_id: str | None = None
        if len(candidate_projects) == 1:
            target_project_id = next(iter(candidate_projects))
        elif current_attr is not None:
            target_project_id = current_attr.project_id
        else:
            fallback = fallback_projects.get(
                normalize_contract_no(raw.linked_sales_order_no), set()
            )
            if len(fallback) == 1:
                target_project_id = next(iter(fallback))
        if target_project_id is None:
            attribution_unowned_skipped += 1
            continue
        if target_project_id not in states:
            raise WorkbookInvalidationConflictError(
                "报销导入期间出现预锁集合外的项目归属，整批未写入，请重试"
            )
        duplicate = session.scalar(
            select(MaintenanceProjectExpenseAttribution.expense_id).where(
                MaintenanceProjectExpenseAttribution.project_id == target_project_id,
                MaintenanceProjectExpenseAttribution.expense_ref == expense_ref_for(raw),
                MaintenanceProjectExpenseAttribution.expense_id
                != expense_id_for(raw_id),
            )
        )
        if duplicate is not None:
            # Never leave an older canonical attribution counting after its raw
            # line changed onto a duplicate business identity.  The database
            # uniqueness constraint would reject the eventual sync anyway; make
            # this a controlled whole-batch conflict instead of silently keeping
            # stale approved cost on the card/workbook.
            attribution_duplicates_skipped += 1
            raise ImportIntegrityError(
                "报销导入出现同项目重复费用单号与序号，整批未写入，请先治理重复行"
            )
        try:
            sync_result = sync_attribution_from_raw(
                session,
                raw=raw,
                project_id=target_project_id,
                status_mapping_version="expense-import-v2",
            )
        except OwnershipConflictError as exc:
            raise ImportIntegrityError(
                "报销历史合同与项目归属冲突，整批未写入，请先治理合同归属"
            ) from exc
        except ExpenseIntegrityError as exc:
            # Once a project owner is known, omitting its canonical row would hide
            # a real completeness gap; for existing rows it would additionally
            # preserve a stale mapped+approved amount.  Keep raw, attribution and
            # workbook revision atomic by rejecting the whole batch.
            raise ImportIntegrityError(
                "报销导入行缺少日期或完整税额，无法安全刷新项目归因，整批未写入"
            ) from exc
        if not sync_result.changed:
            continue
        attributions_synced += 1
        changed_project_ids.update(sync_result.affected_project_ids)
        for project_id in sync_result.affected_project_ids:
            bucket = attribution_counts.setdefault(
                project_id, {"created": 0, "updated": 0}
            )
            bucket["created" if sync_result.created else "updated"] += 1
    for project_id in sorted(changed_project_ids):
        _project_ops._fact_audit(
            session,
            project_id=project_id,
            entity_type="expense",
            entity_id=f"import:{batch_id}",
            action="bulk_sync",
            before=None,
            after=attribution_counts.get(project_id, {}),
            reason=f"报销导入同步项目归因（batch {batch_id}）",
            operated_by=operated_by or "system",
        )
        _project_ops.bump_locked_workbook_revision(
            session, state=states[project_id]
        )

    previous_contracts = {
        row.linked_sales_order_no
        for row in existing_scope.values()
        if row.linked_sales_order_no
    }
    changed_contracts = set(contracts) | previous_contracts
    if changed_contracts and (
        changed_raw_ids or voided
    ):
        _invalidate_expense_snapshot_state(
            session,
            contracts=changed_contracts,
            batch_id=batch_id,
            operated_by=operated_by,
        )
    return {
        "source_rows_total": result.rows_total,
        "fact_rows_inserted": stats["inserted"],
        "fact_rows_updated": stats["updated"],
        "fact_rows_skipped": stats["skipped"],
        "fact_rows_error": sum(1 for e in result.errors if e.error_type not in SOFT_ERROR_TYPES),
        "rows_inactive": result.rows_inactive,
        "expense_rows_replaced": replaced,
        "expense_rows_voided": voided,
        # 「本该作废、但因抑制而保留」的旧行数。>0 ⇒ 删除侧未生效——回执必须显示，
        # 否则用户会以为「以本表为准」已完全生效（作废是减钱动作，留旧账比丢账安全，
        # 但不能不说）。原因见 expense_void_suppressed_reason。
        "expense_rows_void_protected": void_protected,
        "expense_void_suppressed_reason": decision.suppressed_reason,
        # 被排除的无合同行（客户的公司日常开销即此类）：本次未入库，也未牵连旧行。
        "expense_rows_dropped_no_contract": void_inputs.dropped_no_contract,
        # Count canonical rows, not project-side invalidations.  One attribution
        # moved P→Q touches two revisions/audits but is still one synced row.
        "expense_attributions_synced": attributions_synced,
        "expense_attribution_duplicates_skipped": attribution_duplicates_skipped,
        "expense_attribution_unowned_skipped": attribution_unowned_skipped,
        "workbook_projects_bumped": len(changed_project_ids),
        "import_mode": mode,
    }


def _load_inventory(session: Session, result: TransformResult, batch_id: int, snapshot_date: date,
                    operated_by: str | None = None, audit_overwrites: bool = False) -> dict:
    # K3 核查：库存快照只进 inventory 表，不是任何项目工作簿（V2）可见事实——
    # 项目成本口径只由 maintenance_cost.recompute 从采购/销售事实派生，故本路径
    # 不挂 workbook revision 失效（写了也只是冗余 bump）。
    # 1) 商品身份解析（与订单路径同口径：别名/合并重定向）+ alias
    resolution, new_parts = _resolve_line_parts(session, result.inventory, is_sales=False)
    _upsert_aliases(session, result.inventory, resolution)

    # 2) 同 (pn_std, warehouse) 求和（实测 15 组重复，§摸底）。
    #    物理键是 pn_std+warehouse，每组只能落一个 part_id。身份选择规则（确定化，
    #    与行迭代顺序无关）：优先「未被重定向」成员（canonical==组 pn_std）；
    #    若全员重定向，取候选 part_id 最小者。若组内重定向到多个不同 part（极罕见：
    #    两个仅大小写不同的 pn_raw 各自被别名指向不同型号），告警——合并后的数量
    #    会整体记到选中型号，另一型号的库存可见性丢失。
    agg: dict[tuple, dict] = {}
    for r in result.inventory:
        key = (r["pn_std"], r["warehouse"])
        pid, canonical = resolution[r["pn_raw"]]
        is_identity = canonical == r["pn_std"]
        if key not in agg:
            agg[key] = {**r, "source_qty": Decimal("0"),
                        "_part_id": pid, "_identity": is_identity, "_targets": set()}
        g = agg[key]
        g["source_qty"] += r["source_qty"]
        g["_targets"].add(pid)
        # 身份优先级：identity 成员 > 非 identity；同级取 part_id 最小（确定化）
        better = (is_identity and not g["_identity"]) or \
                 (is_identity == g["_identity"] and pid < g["_part_id"])
        if better:
            g["_part_id"], g["_identity"] = pid, is_identity
    for key, g in agg.items():
        if len(g["_targets"]) > 1:
            _log.warning("库存组 %s 重定向到多个型号 %s，数量归到 part_id=%s",
                         key, sorted(g["_targets"]), g["_part_id"])

    rows = [{
        "raw_inventory_id": r["raw_inventory_id"], "part_id": r["_part_id"],
        "pn_std": r["pn_std"], "warehouse": r["warehouse"], "source_qty": r["source_qty"],
        "description": r["description"], "brand": r["brand"],
        "machine_or_part": r["machine_or_part"], "unit": r["unit"],
        "snapshot_date": snapshot_date, "import_batch_id": batch_id,
    } for r in agg.values()]

    # 覆盖留痕（🥉/§3）：库存快照恒覆盖 source_qty（latest-wins），先取既有量，量变则记 before/after。
    before_q: dict[tuple, tuple] = {}
    if audit_overwrites:
        wh_pairs = {(r["pn_std"], r["warehouse"]) for r in rows}
        pn_list = list({r["pn_std"] for r in rows})
        for chunk in _chunks(pn_list):
            for iid, pn, wh, sq, pid in session.execute(
                select(Inventory.id, Inventory.pn_std, Inventory.warehouse,
                       Inventory.source_qty, Inventory.part_id)
                .where(Inventory.pn_std.in_(chunk))
            ).all():
                if (pn, wh) in wh_pairs:
                    before_q[(pn, wh)] = (iid, sq, pid)

    # 3) upsert (pn_std,warehouse)：覆盖 source_qty/snapshot/批次，不动 manual_qty/is_qty_overridden/safety_stock
    for chunk in _chunks(rows):
        stmt = pg_insert(Inventory).values(chunk)
        stmt = stmt.on_conflict_do_update(
            index_elements=[Inventory.pn_std, Inventory.warehouse],
            set_={
                "source_qty": stmt.excluded.source_qty,
                "snapshot_date": stmt.excluded.snapshot_date,
                "part_id": stmt.excluded.part_id,
                "description": stmt.excluded.description, "brand": stmt.excluded.brand,
                "machine_or_part": stmt.excluded.machine_or_part, "unit": stmt.excluded.unit,
                "import_batch_id": stmt.excluded.import_batch_id,
            },
        )
        session.execute(stmt)

    if audit_overwrites and before_q:
        entries = []
        for r in rows:
            prev = before_q.get((r["pn_std"], r["warehouse"]))
            if prev is None:
                continue
            iid, old_qty, old_pid = prev
            # 数量或商品身份(part_id，别名/合并改判后可变)任一变化都留痕（与订单行口径一致）
            if old_qty != r["source_qty"] or old_pid != r["part_id"]:
                entries.append((iid,
                    {"source_qty": _jsonable(old_qty), "part_id": old_pid},
                    {"source_qty": _jsonable(r["source_qty"]), "part_id": r["part_id"],
                     "snapshot_date": _jsonable(snapshot_date)}))
        _audit_overwrites(session, "inventory_overwrite", "inventory", entries, operated_by, batch_id)

    return {
        "source_rows_total": result.rows_total,
        "fact_rows_inserted": len(rows),
        "fact_rows_skipped": 0,
        "fact_rows_error": sum(1 for e in result.errors if e.error_type not in SOFT_ERROR_TYPES),
        "rows_inactive": result.rows_inactive,
        "rows_excluded_warehouse": result.rows_excluded_warehouse,
        "merged_pn_warehouse": len(result.inventory) - len(rows),
        "new_parts": new_parts,
    }
