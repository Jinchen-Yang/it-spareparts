"""车道 B2：回款计划导入 preview/binding/apply/source-file 服务（Task 4 Step 4.3）。

设计依据：``.ai/MAINTENANCE_COLLECTION_REMINDERS_DESIGN.md`` §4.4/§5/§6，
DTO 依据冻结的 ``collection-reminders-api-v1.yaml``（K0 已实现）。

规则摘要：
- preview 零领域事实写入：只创建批次 + 受控原件证据（``raw_file_dir/
  maintenance-collection-plans/`` 下不可猜测 ``storage_key`` 原子写盘，绝不按
  原文件名寻址，不复用 ``BusinessFileLink``）；valid/error 批次都保存 SHA/大小。
- ``operation_key`` 只由 owner 与客户端 Idempotency-Key 规范化生成（不含合同
  版本）；同 key 命中后比较 ``file_sha256`` 与 ``contract_version``，都相同才
  重放，任一不同 409；新 key = 显式重新预览。
- 并发相同 preview 由 ``(owner_user_id, operation_key)`` 唯一键收敛；loser 只
  清理自己未被任何 DB 行引用的文件，绝不删除已有 uploads。
- binding options 只向批次所有者或同权限管理员返回最小字段；q trim 后 ≥2 字符、
  page_size ≤50、绝不返回全量项目；不按项目名自动匹配。
- apply 只读批次冻结 ``plan_json``（绝不重新解析上传文件）；稳定锁顺序
  batch → projects → contracts → bindings → milestones；任一 expected version
  漂移整批 409 且零领域写入；同 payload 重放首次 ``result_json``，不同 409；
  两个并发 apply 最多一个产生领域写入；``(project_contract_id, sequence)``
  create/update/unchanged，source_missing 只报告不删除；修改 handled 节点保留
  handled 并置 ``follow_up_review_required=true``；canary 配置时其他项目整批
  403 / ``canary_scope_denied``；apply 在 ``maintenance_collection_plan_apply_enabled
  =false`` 时失败关闭。
- 改派绑定要求非空理由并写 ``MaintenanceProjectOperationAudit``。
- 原件下载要求同一高风险权限 + 实名 admin + 审计 + attachment disposition。
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.business_time import business_today
from app.config import get_settings
from app.models.maintenance_manager import (
    MaintenanceCollectionMilestone,
    MaintenanceCollectionPlanImportBatch,
    MaintenanceCollectionPlanSourceBinding,
)
from app.models.maintenance_project import (
    MaintenanceProject,
    MaintenanceProjectContract,
)
from app.models.maintenance_project_operations import MaintenanceProjectOperationAudit
from app.models.system import SysUser
from app.schemas.maintenance_collection_reminders import ApplyBinding
from app.security import UserContext, record_access_log
from app.services.maintenance_collection_milestones import write_collection_milestone
from app.services.maintenance_collection_plan_xls import (
    CONTRACT_VERSION,
    CollectionPlanContractError,
    ParsedCollectionPlan,
    parse_project_manager_collection_xls,
)

VALIDATION_TTL = timedelta(hours=24)
STORAGE_DIR_NAME = "maintenance-collection-plans"
IMPORT_ACTION_KEY = "action_maintenance_collection_plan_import"
_SOURCE_SYSTEM = "project_manager_xls_v1"
_SOURCE = "project_manager_xls_v1"
_COMPLETENESS = "complete"


class CollectionPlanImportError(Exception):
    """导入领域错误基类。"""


class CollectionPlanImportInvalid(CollectionPlanImportError):
    """请求或工作簿不符合规则 → 422 invalid_request（可携带 issues）。"""

    def __init__(self, message: str, *, issues: list[dict] | None = None):
        super().__init__(message)
        self.issues = issues or []


class CollectionPlanImportPermissionError(CollectionPlanImportError):
    """无权限 → 403 permission_denied。"""


class CollectionPlanImportCanaryDenied(CollectionPlanImportError):
    """灰度期间仅允许 canary 项目 → 403 canary_scope_denied。"""


class CollectionPlanImportNotFound(CollectionPlanImportError):
    """资源不存在或不可见 → 404 not_found。"""


class CollectionPlanImportConflict(CollectionPlanImportError):
    """版本/幂等冲突 → 409 version_conflict。"""

    def __init__(
        self,
        message: str,
        *,
        current_version: int | None = None,
        current_data_version: str | None = None,
    ):
        super().__init__(message)
        self.current_version = current_version
        self.current_data_version = current_data_version


@dataclass(frozen=True)
class CollectionPlanSourceFile:
    """原件下载结果：存储路径 + 审计元信息（绝不包含业务行）。"""

    storage_path: Path
    filename: str
    sha256: str
    file_size: int
    content_type: str = "application/vnd.ms-excel"


# ---------- 实名账号门禁 ----------

def require_import_operator(db: Session, *, user_ctx: UserContext) -> SysUser:
    """导入写门（设计 §9）：实名 admin + 显式 import action + data_profit。

    与 API 层同一语义：admin 不得短路，只认账号快照⊕覆盖；每次执行前重读账号
    避免撤权 TOCTOU。
    """
    from app import permissions as _perm

    if not user_ctx.is_authenticated or not user_ctx.user_id:
        raise CollectionPlanImportPermissionError("请先登录")
    user = db.scalar(
        select(SysUser).where(
            SysUser.username == user_ctx.user_id,
            SysUser.is_active.is_(True),
        )
    )
    if user is None:
        raise CollectionPlanImportPermissionError("账号不存在或已停用")
    if user.role != "admin":
        raise CollectionPlanImportPermissionError("回款计划导入能力仅限实名管理员")
    graph = _perm.effective_for_user(user)
    if not graph.get(IMPORT_ACTION_KEY, False):
        raise CollectionPlanImportPermissionError("未显式授予回款计划导入权限")
    if not _perm.runtime_safe(graph).get("data_profit", False):
        raise CollectionPlanImportPermissionError(
            "回款计划导入能力要求同时具备利润数据可见权限"
        )
    return user


def _is_admin_with_import(user: SysUser) -> bool:
    from app import permissions as _perm

    if user.role != "admin":
        return False
    graph = _perm.effective_for_user(user)
    return bool(graph.get(IMPORT_ACTION_KEY, False))


# ---------- 稳定派生工具 ----------

def _canonical_json(value) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _stable_hash(value) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _operation_key(owner_user_id: int, idempotency_key: str) -> str:
    """预览幂等键：只由 owner 与客户端 Idempotency-Key 生成，绝不含合同版本。"""
    raw = f"collection-plan-preview:{owner_user_id}\x00{idempotency_key}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _apply_payload_hash(
    expected_batch_version: int,
    expected_data_version: str,
    bindings: list[ApplyBinding],
) -> str:
    """apply payload hash：expected_batch_version + expected_data_version + sorted(bindings)。"""
    payload = {
        "expected_batch_version": expected_batch_version,
        "expected_data_version": expected_data_version,
        "bindings": [
            binding.model_dump(mode="json")
            for binding in sorted(bindings, key=lambda item: item.row_key)
        ],
    }
    return _stable_hash(payload)


def _storage_dir() -> Path:
    return Path(get_settings().raw_file_dir) / STORAGE_DIR_NAME


def _write_evidence(content: bytes, *, storage_key: str) -> Path:
    """按不可猜测 key 原子写盘（临时文件 + rename）；绝不按原文件名寻址。"""
    directory = _storage_dir()
    directory.mkdir(parents=True, exist_ok=True)
    final_path = directory / storage_key
    temp_path = directory / f".{storage_key}.tmp-{uuid4().hex[:8]}"
    temp_path.write_bytes(content)
    os.replace(temp_path, final_path)
    return final_path


def _delete_unreferenced_evidence(db: Session, *, storage_key: str) -> None:
    """并发 loser 只清理自己尚未被任何 DB 行引用的临时文件。"""
    referenced = db.scalar(
        select(func.count())
        .select_from(MaintenanceCollectionPlanImportBatch)
        .where(MaintenanceCollectionPlanImportBatch.storage_key == storage_key)
    )
    if referenced:
        return
    path = _storage_dir() / storage_key
    try:
        path.unlink(missing_ok=True)
    except OSError:  # noqa: S110
        pass


def _load_batch(db: Session, batch_id: str) -> MaintenanceCollectionPlanImportBatch:
    batch = db.scalar(
        select(MaintenanceCollectionPlanImportBatch).where(
            MaintenanceCollectionPlanImportBatch.batch_id == batch_id
        )
    )
    if batch is None:
        raise CollectionPlanImportNotFound("资源不存在或不可见")
    return batch


def _visible_batch(
    db: Session,
    batch_id: str,
    *,
    user: SysUser,
    owner_user_id: int,
    allow_other_admin: bool = False,
) -> MaintenanceCollectionPlanImportBatch:
    batch = _load_batch(db, batch_id)
    if batch.owner_user_id == owner_user_id:
        return batch
    if allow_other_admin and _is_admin_with_import(user):
        return batch
    raise CollectionPlanImportNotFound("资源不存在或不可见")


def _issue_payload(issue: dict) -> dict:
    """映射为冻结 ImportIssue 形状（code/severity/row_key/sequence/message）。"""
    return {
        "code": str(issue.get("code") or "import_issue"),
        "severity": str(issue.get("severity") or "blocker"),
        "row_key": issue.get("row_key"),
        "sequence": issue.get("sequence"),
        "message": str(issue.get("message") or "导入校验问题"),
    }


# ---------- preview ----------

def preview_collection_plan_import(
    db: Session,
    *,
    content: bytes,
    filename: str,
    idempotency_key: str,
    owner_user_id: int,
    operator: str,
    user_ctx: UserContext,
    as_of: date,
) -> dict:
    """零领域事实写入预览：只创建批次与受控原件证据。"""
    if not idempotency_key or not (8 <= len(idempotency_key) <= 128):
        raise CollectionPlanImportInvalid("Idempotency-Key 必须为 8–128 字符")
    operator = _audit_operator(operator)
    user = require_import_operator(db, user_ctx=user_ctx)
    if user.id != owner_user_id:
        raise CollectionPlanImportPermissionError("预览账号与实名账号不一致")

    digest = hashlib.sha256(content).hexdigest()
    operation_key = _operation_key(owner_user_id, idempotency_key)

    existing = db.scalar(
        select(MaintenanceCollectionPlanImportBatch).where(
            MaintenanceCollectionPlanImportBatch.owner_user_id == owner_user_id,
            MaintenanceCollectionPlanImportBatch.operation_key == operation_key,
        )
    )
    if existing is not None:
        # 同 key 命中：文件与合同版本都相同才重放首次结果，任一不同 409。
        if (
            existing.file_sha256 == digest
            and existing.contract_version == CONTRACT_VERSION
        ):
            return _replay_preview(existing)
        raise CollectionPlanImportConflict(
            "该幂等键已用于不同文件或合同版本，请更换键重新预览",
            current_version=existing.version,
            current_data_version=existing.data_version,
        )

    # 解析（只读）；合同级失败关闭仍保留哈希与受控原件证据。
    try:
        parsed = parse_project_manager_collection_xls(content, filename=filename)
    except CollectionPlanContractError as exc:
        return _create_error_batch(
            db,
            content=content,
            filename=filename,
            digest=digest,
            operation_key=operation_key,
            owner_user_id=owner_user_id,
            operator=operator,
            contract_error=exc,
        )

    plan_payload, data_version = _build_plan_payload(db, parsed, as_of=as_of)
    issues = [_issue_payload(issue) for issue in parsed.issues]
    blockers = [issue for issue in issues if issue["severity"] == "blocker"]
    status = "valid" if not blockers else "error"

    storage_key = uuid4().hex
    batch = MaintenanceCollectionPlanImportBatch(
        batch_id=uuid4().hex,
        owner_user_id=owner_user_id,
        contract_version=CONTRACT_VERSION,
        file_sha256=digest,
        file_size=len(content),
        original_filename=str(filename or "")[:255],
        storage_key=storage_key,
        operation_key=operation_key,
        semantic_hash=parsed.semantic_hash,
        data_version=data_version,
        version=1,
        status=status,
        plan_json=plan_payload,
        issues_json=issues,
        created_by=operator,
        expires_at=datetime.now(UTC) + VALIDATION_TTL,
    )
    try:
        _write_evidence(content, storage_key=storage_key)
        db.add(batch)
        db.flush()
    except IntegrityError:
        db.rollback()
        _delete_unreferenced_evidence(db, storage_key=storage_key)
        winner = db.scalar(
            select(MaintenanceCollectionPlanImportBatch).where(
                MaintenanceCollectionPlanImportBatch.owner_user_id == owner_user_id,
                MaintenanceCollectionPlanImportBatch.operation_key == operation_key,
            )
        )
        if winner is None:
            raise
        if winner.file_sha256 == digest and winner.contract_version == CONTRACT_VERSION:
            return _replay_preview(winner)
        raise CollectionPlanImportConflict(
            "并发预览使用了不同的文件内容，请更换键重新预览",
            current_version=winner.version,
            current_data_version=winner.data_version,
        )
    return _preview_payload(batch)


def _audit_operator(operator: str) -> str:
    value = str(operator or "").strip()[:64]
    if not value:
        raise CollectionPlanImportPermissionError("缺少可审计的操作人")
    return value


def _create_error_batch(
    db: Session,
    *,
    content: bytes,
    filename: str,
    digest: str,
    operation_key: str,
    owner_user_id: int,
    operator: str,
    contract_error: CollectionPlanContractError,
) -> dict:
    """合同级失败：仍保存哈希 + 受控原件证据（error 批次），随后抛 422。"""
    issue = {
        "code": contract_error.code,
        "severity": "blocker",
        "row_key": None,
        "sequence": None,
        "message": contract_error.message,
    }
    storage_key = uuid4().hex
    batch = MaintenanceCollectionPlanImportBatch(
        batch_id=uuid4().hex,
        owner_user_id=owner_user_id,
        contract_version=CONTRACT_VERSION,
        file_sha256=digest,
        file_size=len(content),
        original_filename=str(filename or "")[:255],
        storage_key=storage_key,
        operation_key=operation_key,
        semantic_hash="",
        data_version="",
        version=1,
        status="error",
        plan_json=None,
        issues_json=[issue],
        created_by=operator,
        expires_at=datetime.now(UTC) + VALIDATION_TTL,
    )
    try:
        _write_evidence(content, storage_key=storage_key)
        db.add(batch)
        db.flush()
    except IntegrityError:
        db.rollback()
        _delete_unreferenced_evidence(db, storage_key=storage_key)
        winner = db.scalar(
            select(MaintenanceCollectionPlanImportBatch).where(
                MaintenanceCollectionPlanImportBatch.owner_user_id == owner_user_id,
                MaintenanceCollectionPlanImportBatch.operation_key == operation_key,
            )
        )
        if winner is not None:
            return _replay_preview(winner)
        raise
    raise CollectionPlanImportInvalid(contract_error.message, issues=[issue])


def _replay_preview(batch: MaintenanceCollectionPlanImportBatch) -> dict:
    """同 key + 同文件 + 同合同版本 → 重放首次预览结果（或合同错误）。"""
    if batch.status == "error" and batch.plan_json is None:
        issues = [_issue_payload(issue) for issue in batch.issues_json]
        raise CollectionPlanImportInvalid("工作簿不符合合同", issues=issues)
    return _preview_payload(batch)


def _build_plan_payload(
    db: Session, parsed: ParsedCollectionPlan, *, as_of: date
) -> tuple[dict, str]:
    """把 parser 输出与预览时数据库状态合并为不可变 plan_json + data_version。

    plan_json 只保存受控字段（绑定/版本/规范化计划），绝不保存整行 raw JSON。
    data_version 覆盖项目/合同/绑定/节点的 expected versions。
    """
    orders: list[dict] = []
    versions: dict[str, dict] = {"projects": {}, "contracts": {}, "bindings": {}, "milestones": {}}
    for row in parsed.rows:
        binding = db.scalar(
            select(MaintenanceCollectionPlanSourceBinding).where(
                MaintenanceCollectionPlanSourceBinding.source_system == _SOURCE_SYSTEM,
                MaintenanceCollectionPlanSourceBinding.external_order_no
                == row.external_order_no,
            )
        )
        binding_payload: dict | None = None
        nodes_payload: list[dict] = []
        if binding is not None:
            binding_payload = {
                "status": "reviewed",
                "project_id": binding.project_id,
                "project_version": None,
                "project_contract_id": binding.project_contract_id,
                "project_contract_version": None,
                "existing_binding_version": binding.version,
                "binding_id": binding.binding_id,
            }
            project = db.get(MaintenanceProject, binding.project_id)
            contract = db.get(
                MaintenanceProjectContract, binding.project_contract_id
            )
            if project is not None:
                binding_payload["project_version"] = project.version
                versions["projects"][project.project_id] = project.version
            if contract is not None:
                binding_payload["project_contract_version"] = contract.version
                versions["contracts"][contract.project_contract_id] = contract.version
            versions["bindings"][row.external_order_no] = binding.version
            existing_milestones = {
                milestone.sequence: milestone
                for milestone in db.scalars(
                    select(MaintenanceCollectionMilestone).where(
                        MaintenanceCollectionMilestone.project_contract_id
                        == binding.project_contract_id
                    )
                )
            }
            for node in row.nodes:
                current = existing_milestones.get(node.sequence)
                change = _node_change(current, node)
                nodes_payload.append(
                    {
                        "sequence": node.sequence,
                        "planned_month": node.planned_month,
                        "planned_amount": node.planned_amount,
                        "expected_milestone_version": (
                            current.version if current is not None else None
                        ),
                        "change": change,
                    }
                )
                if current is not None:
                    versions["milestones"][
                        f"{binding.project_contract_id}:{node.sequence}"
                    ] = current.version
            # source_missing：既有 XLS 节点不在新计划中 → 预览期差异只报告，绝不删除。
            plan_sequences = {node.sequence for node in row.nodes}
            source_missing = [
                {
                    "sequence": milestone.sequence,
                    "expected_milestone_version": milestone.version,
                }
                for milestone in existing_milestones.values()
                if milestone.source == _SOURCE
                and milestone.sequence not in plan_sequences
            ]
        else:
            binding_payload = {
                "status": "pending_review",
                "project_id": None,
                "project_version": None,
                "project_contract_id": None,
                "project_contract_version": None,
                "existing_binding_version": None,
                "binding_id": None,
            }
            for node in row.nodes:
                nodes_payload.append(
                    {
                        "sequence": node.sequence,
                        "planned_month": node.planned_month,
                        "planned_amount": node.planned_amount,
                        "expected_milestone_version": None,
                        "change": "create",
                    }
                )
            source_missing = []
        orders.append(
            {
                "row_key": row.row_key,
                "external_order_no": row.external_order_no,
                "source_project_name": row.source_project_name,
                "order_amount": row.order_amount,
                "plan_total": row.plan_total,
                "warning_codes": list(row.warning_codes),
                "blocker_codes": list(row.blocker_codes),
                "binding": binding_payload,
                "nodes": nodes_payload,
                "source_missing": source_missing,
            }
        )

    plan_payload = {
        "contract_version": parsed.contract_version,
        "semantic_hash": parsed.semantic_hash,
        "orders": orders,
    }
    data_version = _stable_hash(
        {
            "contract_version": parsed.contract_version,
            "semantic_hash": parsed.semantic_hash,
            "versions": versions,
        }
    )
    return plan_payload, data_version


def _preview_payload(batch: MaintenanceCollectionPlanImportBatch) -> dict:
    """把批次落库状态装配为冻结 PreviewResponse（200 形状）。"""
    plan = batch.plan_json or {"orders": []}
    rows = []
    counts = {
        "projects": 0,
        "milestones": 0,
        "bound": 0,
        "pending_binding": 0,
        "blockers": 0,
        "warnings": 0,
        "create": 0,
        "update": 0,
        "unchanged": 0,
        "source_missing": 0,
    }
    for order in plan["orders"]:
        binding = order["binding"]
        blocker_codes = order.get("blocker_codes") or []
        warning_codes = order.get("warning_codes") or []
        binding_status = binding.get("status") if binding else "pending_review"
        if binding_status == "reviewed":
            counts["bound"] += 1
        elif not blocker_codes:
            counts["pending_binding"] += 1
        counts["blockers"] += len(blocker_codes)
        counts["warnings"] += len(warning_codes)
        milestone_diffs = []
        for node in order["nodes"]:
            counts["milestones"] += 1
            change = node.get("change") or "create"
            milestone_diffs.append(
                {
                    "sequence": node["sequence"],
                    "planned_month": node["planned_month"],
                    "planned_amount": node["planned_amount"],
                    "change": change,
                    "expected_milestone_version": node.get("expected_milestone_version"),
                }
            )
            counts[change] += 1
        for missing in order.get("source_missing") or []:
            milestone_diffs.append(
                {
                    "sequence": missing["sequence"],
                    "planned_month": None,
                    "planned_amount": None,
                    "change": "source_missing",
                    "expected_milestone_version": missing.get("expected_milestone_version"),
                }
            )
            counts["source_missing"] += 1
        rows.append(
            {
                "row_key": order["row_key"],
                "external_order_no": order["external_order_no"],
                "source_project_name": order.get("source_project_name"),
                "binding": {
                    "status": binding_status,
                    "project_id": binding.get("project_id") if binding else None,
                    "project_version": binding.get("project_version") if binding else None,
                    "project_contract_id": binding.get("project_contract_id") if binding else None,
                    "project_contract_version": binding.get("project_contract_version") if binding else None,
                    "existing_binding_version": binding.get("existing_binding_version") if binding else None,
                },
                "milestone_diffs": milestone_diffs,
                "warning_codes": warning_codes,
                "blocker_codes": blocker_codes,
            }
        )
    counts["projects"] = len(
        {order.get("source_project_name") for order in plan["orders"] if order.get("source_project_name")}
    )
    issues = [_issue_payload(issue) for issue in batch.issues_json]
    return {
        "batch_id": batch.batch_id,
        "batch_version": batch.version,
        "data_version": batch.data_version,
        "status": batch.status,
        "contract_version": batch.contract_version,
        "file_sha256": batch.file_sha256,
        "counts": counts,
        "rows": rows,
        "issues": issues,
        "can_apply": batch.status == "valid",
        "expires_at": batch.expires_at,
    }


def _node_change(current, node) -> str:
    """预览期差异分类：计划事实（日期/金额/完整度/精度）未变 → unchanged。"""
    if current is None:
        return "create"
    planned_date = date.fromisoformat(node.planned_month + "-01")
    if (
        current.planned_date == planned_date
        and Decimal(str(current.planned_amount)) == Decimal(node.planned_amount)
        and current.completeness_state == _COMPLETENESS
        and current.date_precision == "month"
    ):
        return "unchanged"
    return "update"


# ---------- binding options ----------

def search_collection_binding_options(
    db: Session,
    *,
    batch_id: str,
    q_text: str,
    page: int,
    page_size: int,
    user_ctx: UserContext,
) -> dict:
    """受控项目/合同候选搜索：绝不返回全量项目，不做名称自动匹配。"""
    user = require_import_operator(db, user_ctx=user_ctx)
    _visible_batch(db, batch_id, user=user, owner_user_id=user.id, allow_other_admin=True)
    q = str(q_text or "").strip()
    if len(q) < 2:
        raise CollectionPlanImportInvalid("搜索词去除首尾空白后至少 2 个字符")
    if page_size < 1 or page_size > 50:
        raise CollectionPlanImportInvalid("page_size 必须在 1–50 之间")
    if page < 1:
        raise CollectionPlanImportInvalid("page 必须大于等于 1")
    like = f"%{q}%"
    base = (
        select(MaintenanceProject)
        .where(
            MaintenanceProject.is_active.is_(True),
            or_(
                MaintenanceProject.project_code.ilike(like),
                MaintenanceProject.display_name.ilike(like),
            ),
        )
        .order_by(MaintenanceProject.project_code, MaintenanceProject.project_id)
    )
    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0
    projects = list(db.scalars(base.limit(page_size).offset((page - 1) * page_size)))
    project_ids = [project.project_id for project in projects]
    contracts_by_project: dict[str, list[MaintenanceProjectContract]] = {}
    if project_ids:
        contract_rows = list(
            db.scalars(
                select(MaintenanceProjectContract)
                .where(MaintenanceProjectContract.project_id.in_(project_ids))
                .order_by(
                    MaintenanceProjectContract.project_id,
                    MaintenanceProjectContract.contract_no,
                    MaintenanceProjectContract.project_contract_id,
                )
            )
        )
        for contract in contract_rows:
            contracts_by_project.setdefault(contract.project_id, []).append(contract)
    rows = []
    as_of = business_today()
    for project in projects:
        rows.append(
            {
                "project_id": project.project_id,
                "project_code": project.project_code,
                "display_name": project.display_name,
                "version": project.version,
                "contracts": [
                    _binding_option_contract(contract, as_of=as_of)
                    for contract in contracts_by_project.get(project.project_id, [])
                ],
            }
        )
    return {
        "batch_id": batch_id,
        "rows": rows,
        "total": total,
        "page": page,
        "page_size": page_size,
        "q": q,
    }


def _binding_option_contract(
    contract: MaintenanceProjectContract, *, as_of: date
) -> dict:
    relation_status = "active" if contract.effective_to is None else "archived"
    if contract.effective_from > as_of:
        lifecycle_status = "upcoming"
    elif contract.effective_to is not None and contract.effective_to < as_of:
        lifecycle_status = "ended"
    else:
        lifecycle_status = "active"
    return {
        "project_contract_id": contract.project_contract_id,
        "contract_no": contract.contract_no,
        "relation_status": relation_status,
        "lifecycle_status": lifecycle_status,
        "version": contract.version,
    }


# ---------- apply ----------

def apply_collection_plan_import(
    db: Session,
    *,
    batch_id: str,
    expected_batch_version: int,
    expected_data_version: str,
    bindings: list[ApplyBinding],
    owner_user_id: int,
    operator: str,
    user_ctx: UserContext,
    as_of: date,
) -> dict:
    """原子应用：只读冻结 plan_json；整批版本校验通过才写，任一漂移整批 409。"""
    settings = get_settings()
    if not settings.maintenance_collection_plan_apply_enabled:
        raise CollectionPlanImportPermissionError("回款计划应用尚未开放")
    operator = _audit_operator(operator)
    user = require_import_operator(db, user_ctx=user_ctx)
    if user.id != owner_user_id:
        raise CollectionPlanImportPermissionError("应用账号与实名账号不一致")
    batch = db.scalar(
        select(MaintenanceCollectionPlanImportBatch)
        .where(MaintenanceCollectionPlanImportBatch.batch_id == batch_id)
        .with_for_update()
    )
    if batch is None or batch.owner_user_id != owner_user_id:
        raise CollectionPlanImportNotFound("资源不存在或不可见")
    if batch.expires_at < datetime.now(UTC):
        raise CollectionPlanImportConflict(
            "预览批次已过期，请重新预览",
            current_version=batch.version,
            current_data_version=batch.data_version,
        )

    payload_hash = _apply_payload_hash(
        expected_batch_version, expected_data_version, bindings
    )
    if batch.status == "applied":
        if batch.apply_payload_hash == payload_hash:
            return _replay_apply(batch)
        raise CollectionPlanImportConflict(
            "同一批次已用不同参数应用，请重新预览",
            current_version=batch.version,
            current_data_version=batch.data_version,
        )
    if batch.status != "valid":
        raise CollectionPlanImportInvalid("批次当前不可应用，请重新预览")
    if batch.version != expected_batch_version:
        raise CollectionPlanImportConflict(
            "批次版本已变化，请刷新后重试",
            current_version=batch.version,
            current_data_version=batch.data_version,
        )
    if batch.data_version != expected_data_version:
        raise CollectionPlanImportConflict(
            "数据版本已变化，请刷新后重试",
            current_version=batch.version,
            current_data_version=batch.data_version,
        )

    plan = batch.plan_json or {"orders": []}
    if settings.maintenance_collection_canary_project_id:
        for binding in bindings:
            if binding.project_id != settings.maintenance_collection_canary_project_id:
                raise CollectionPlanImportCanaryDenied(
                    "灰度期间仅允许 canary 项目应用回款计划"
                )

    plan_orders = {order["row_key"]: order for order in plan["orders"]}
    bindable_orders = [
        order for order in plan["orders"] if not order.get("blocker_codes")
    ]
    if any(order.get("blocker_codes") for order in plan["orders"]):
        raise CollectionPlanImportInvalid("计划存在阻断项，不能应用")
    if len(bindings) != len(bindable_orders):
        raise CollectionPlanImportInvalid("绑定数量与待绑定订单不一致")
    for binding in bindings:
        order = plan_orders.get(binding.row_key)
        if order is None:
            raise CollectionPlanImportInvalid("存在未在计划中的绑定行")
        if order["external_order_no"] != binding.external_order_no:
            raise CollectionPlanImportInvalid("绑定订单编号与计划不一致")

    # 稳定锁顺序：batch（已锁）→ projects → contracts → bindings → milestones。
    project_ids = {binding.project_id for binding in bindings}
    contract_ids = {binding.project_contract_id for binding in bindings}
    projects = {
        project.project_id: project
        for project in db.scalars(
            select(MaintenanceProject)
            .where(MaintenanceProject.project_id.in_(project_ids))
            .with_for_update()
        )
    }
    contracts = {
        contract.project_contract_id: contract
        for contract in db.scalars(
            select(MaintenanceProjectContract)
            .where(MaintenanceProjectContract.project_contract_id.in_(contract_ids))
            .with_for_update()
        )
    }
    binding_rows = {
        row.external_order_no: row
        for row in db.scalars(
            select(MaintenanceCollectionPlanSourceBinding)
            .where(
                MaintenanceCollectionPlanSourceBinding.source_system == _SOURCE_SYSTEM,
                MaintenanceCollectionPlanSourceBinding.external_order_no.in_(
                    {binding.external_order_no for binding in bindings}
                ),
            )
            .with_for_update()
        )
    }
    milestones = {
        (milestone.project_contract_id, milestone.sequence): milestone
        for milestone in db.scalars(
            select(MaintenanceCollectionMilestone)
            .where(
                MaintenanceCollectionMilestone.project_contract_id.in_(contract_ids)
            )
            .with_for_update()
        )
    }

    counts = {
        "created": 0,
        "updated": 0,
        "unchanged": 0,
        "source_missing": 0,
        "needs_review": 0,
    }
    now = datetime.now(UTC)
    for binding in bindings:
        order = plan_orders[binding.row_key]
        project = projects.get(binding.project_id)
        if project is None or not project.is_active:
            raise CollectionPlanImportNotFound("项目不存在或不可见")
        if project.version != binding.project_version:
            raise CollectionPlanImportConflict(
                "项目版本已变化，请重新预览",
                current_version=batch.version,
                current_data_version=batch.data_version,
            )
        contract = contracts.get(binding.project_contract_id)
        if contract is None or contract.project_id != binding.project_id:
            raise CollectionPlanImportNotFound("合同不存在或不属于所选项目")
        if contract.effective_from > as_of or (
            contract.effective_to is not None and contract.effective_to <= as_of
        ):
            raise CollectionPlanImportInvalid("所选合同当前不在有效期内")
        if contract.version != binding.project_contract_version:
            raise CollectionPlanImportConflict(
                "合同版本已变化，请重新预览",
                current_version=batch.version,
                current_data_version=batch.data_version,
            )

        existing = binding_rows.get(binding.external_order_no)
        if existing is None:
            if binding.existing_binding_version is not None:
                raise CollectionPlanImportConflict(
                    "绑定版本已变化，请重新预览",
                    current_version=batch.version,
                    current_data_version=batch.data_version,
                )
            db.add(
                MaintenanceCollectionPlanSourceBinding(
                    binding_id=uuid4().hex,
                    source_system=_SOURCE_SYSTEM,
                    external_order_no=binding.external_order_no,
                    project_id=binding.project_id,
                    project_contract_id=binding.project_contract_id,
                    binding_status="reviewed",
                    reviewed_by=user.id,
                    reviewed_at=now,
                    version=1,
                )
            )
            binding_rows[binding.external_order_no] = None  # 占位避免重复创建
        else:
            if existing.version != binding.existing_binding_version:
                raise CollectionPlanImportConflict(
                    "绑定版本已变化，请重新预览",
                    current_version=batch.version,
                    current_data_version=batch.data_version,
                )
            if (
                existing.project_id != binding.project_id
                or existing.project_contract_id != binding.project_contract_id
            ):
                reason = (binding.reason or "").strip()
                if not reason:
                    raise CollectionPlanImportInvalid("改派绑定必须填写理由")
                db.add(
                    MaintenanceProjectOperationAudit(
                        project_id=binding.project_id,
                        entity_type="collection_plan_source_binding",
                        entity_id=existing.binding_id,
                        action="reassign",
                        before_json={
                            "project_id": existing.project_id,
                            "project_contract_id": existing.project_contract_id,
                            "version": existing.version,
                        },
                        after_json={
                            "project_id": binding.project_id,
                            "project_contract_id": binding.project_contract_id,
                        },
                        reason=reason,
                        operated_by=operator,
                    )
                )
                existing.project_id = binding.project_id
                existing.project_contract_id = binding.project_contract_id
                existing.version += 1
                existing.reviewed_by = user.id
                existing.reviewed_at = now

        # 节点：逐对 (project_contract_id, sequence) create/update/unchanged。
        plan_binding = order.get("binding") or {}
        reassigning = bool(
            plan_binding.get("project_contract_id")
            and plan_binding.get("project_contract_id") != binding.project_contract_id
        )
        for node in order["nodes"]:
            current = milestones.get((binding.project_contract_id, node["sequence"]))
            expected = node.get("expected_milestone_version")
            # 改派到新合同：预览期版本前提属于旧合同，用户已显式选择并确认新合同。
            # 如果目标合同已存在同序号节点，当前 apply payload 没有该目标节点的
            # expected_milestone_version；失败关闭，避免无版本前提覆盖目标计划。
            if reassigning and current is not None:
                raise CollectionPlanImportConflict(
                    "目标合同已有同序号计划节点，请重新预览后处理",
                    current_version=batch.version,
                    current_data_version=batch.data_version,
                )
            if expected is not None and not reassigning:
                if current is None or current.version != expected:
                    raise CollectionPlanImportConflict(
                        "计划节点版本已变化，请重新预览",
                        current_version=batch.version,
                        current_data_version=batch.data_version,
                    )
            was_handled = current is not None and current.follow_up_status == "handled"
            planned_date = date.fromisoformat(node["planned_month"] + "-01")
            # 写 helper 会原地更新 identity map 中的节点对象：分类必须先拍快照。
            if current is not None:
                original_facts = (
                    current.planned_date,
                    Decimal(str(current.planned_amount)),
                    current.completeness_state,
                    current.date_precision,
                )
            else:
                original_facts = None
            write_collection_milestone(
                db,
                project_id=binding.project_id,
                project_contract_id=binding.project_contract_id,
                sequence=node["sequence"],
                planned_date=planned_date,
                planned_amount=node["planned_amount"],
                completeness_state=_COMPLETENESS,
                source=_SOURCE,
                collection_plan_import_batch_id=batch.batch_id,
                date_precision="month",
                operator=operator,
            )
            if original_facts is None:
                counts["created"] += 1
            elif original_facts == (
                planned_date,
                Decimal(node["planned_amount"]),
                _COMPLETENESS,
                "month",
            ):
                counts["unchanged"] += 1
            else:
                counts["updated"] += 1
                if was_handled:
                    counts["needs_review"] += 1

        # source_missing：旧 XLS 节点不在新计划中 → 只报告，绝不删除。
        for (contract_id, sequence), milestone in milestones.items():
            if contract_id != binding.project_contract_id:
                continue
            if milestone.source == _SOURCE and sequence not in {
                node["sequence"] for node in order["nodes"]
            }:
                counts["source_missing"] += 1

    batch.version += 1
    batch.status = "applied"
    batch.apply_payload_hash = payload_hash
    batch.result_json = {
        "counts": counts,
    }
    batch.applied_by = operator
    batch.applied_at = now
    db.flush()
    return {
        "batch_id": batch.batch_id,
        "batch_version": batch.version,
        "data_version": batch.data_version,
        "status": batch.status,
        "counts": counts,
        "idempotent_replay": False,
        "applied_at": now,
    }


def _replay_apply(batch: MaintenanceCollectionPlanImportBatch) -> dict:
    """已应用批次 + 相同 payload → 重放首次 result_json，零新写入。"""
    counts = (batch.result_json or {}).get("counts") or {
        "created": 0,
        "updated": 0,
        "unchanged": 0,
        "source_missing": 0,
        "needs_review": 0,
    }
    return {
        "batch_id": batch.batch_id,
        "batch_version": batch.version,
        "data_version": batch.data_version,
        "status": batch.status,
        "counts": counts,
        "idempotent_replay": True,
        "applied_at": batch.applied_at or datetime.now(UTC),
    }


def _money_text(value) -> str:
    """金额统一走 Decimal 字符串比较，绝不经过浮点。"""
    if value is None:
        return ""
    return format(Decimal(str(value)), "f")


# ---------- source file ----------

def open_collection_plan_source_file(
    db: Session,
    *,
    batch_id: str,
    owner_user_id: int,
    operator: str,
    user_ctx: UserContext,
) -> CollectionPlanSourceFile:
    """原件下载：同一高风险权限 + 实名 admin；写审计；返回受控存储路径。"""
    operator = _audit_operator(operator)
    user = require_import_operator(db, user_ctx=user_ctx)
    batch = _visible_batch(
        db, batch_id, user=user, owner_user_id=owner_user_id, allow_other_admin=True
    )
    path = _storage_dir() / batch.storage_key
    if not path.is_file():
        raise CollectionPlanImportNotFound("原件不存在或不可见")
    # 审计：绑定项目存在时写项目操作审计；普通访问日志不含文件名与业务行。
    plan = batch.plan_json or {"orders": []}
    project_id = None
    for order in plan["orders"]:
        binding = order.get("binding") or {}
        if binding.get("project_id"):
            project_id = binding["project_id"]
            break
    if project_id is not None:
        db.add(
            MaintenanceProjectOperationAudit(
                project_id=project_id,
                entity_type="collection_plan_import_batch",
                entity_id=batch.batch_id,
                action="source_file_download",
                before_json=None,
                after_json={"file_sha256": batch.file_sha256, "file_size": batch.file_size},
                reason="回款计划原件下载审计",
                operated_by=operator,
            )
        )
    record_access_log(
        user_ctx,
        "collection_plan_source_file_download",
        f"collection_plan_import_batch:{batch.batch_id}",
        {"file_size": batch.file_size, "sha256_prefix": batch.file_sha256[:12]},
    )
    return CollectionPlanSourceFile(
        storage_path=path,
        filename=batch.original_filename,
        sha256=batch.file_sha256,
        file_size=batch.file_size,
    )
