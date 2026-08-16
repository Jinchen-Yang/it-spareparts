"""回款提醒凭证（F6）：上传 → 独立目录文件 + yml sidecar + DB 只记 md5。

复用 business_file 元数据层（sha256/大小/MIME/上传人）与验收附件校验
（validate_attachment：扩展名/MIME/内容一致性、20MB 上限、PDF 脚本剥离检测）。
"""
from __future__ import annotations

import hashlib
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from uuid import uuid4

import yaml
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.maintenance_collection_evidence import (
    MaintenanceCollectionEvidence,
)
from app.models.maintenance_manager import (
    BusinessFile,
    MaintenanceCollectionMilestone,
)
from app.services.maintenance_acceptance import validate_attachment
from sqlalchemy.exc import IntegrityError


class CollectionEvidenceError(RuntimeError):
    """回款凭证业务错误。"""


def _root() -> Path:
    root = Path(get_settings().raw_file_dir).resolve() / "collection_evidence"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return root


def _resolved_path(object_key: str) -> Path:
    relative = PurePosixPath(object_key)
    if (
        relative.is_absolute()
        or ".." in relative.parts
        or not relative.parts
        or relative.parts[:1] != ("collection_evidence",)
    ):
        raise CollectionEvidenceError("凭证存储路径无效")
    raw_root = Path(get_settings().raw_file_dir).resolve()
    target = (raw_root / Path(*relative.parts)).resolve()
    try:
        target.relative_to(_root())
    except ValueError as exc:
        raise CollectionEvidenceError("凭证存储路径越界") from exc
    return target


def _write_meta_sidecar(file_id: str, meta: dict) -> None:
    path = _root() / file_id[:2] / f"{file_id}.yml"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temp_name = tempfile.mkstemp(prefix=".meta-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            yaml.safe_dump(meta, handle, allow_unicode=True, sort_keys=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def active_evidence_count(db: Session, milestone_id: str) -> int:
    return int(
        db.scalar(
            select(func.count())
            .select_from(MaintenanceCollectionEvidence)
            .where(
                MaintenanceCollectionEvidence.milestone_id == milestone_id,
                MaintenanceCollectionEvidence.is_active.is_(True),
            )
        )
        or 0
    )


def list_evidence(db: Session, milestone_id: str) -> list[dict]:
    rows = db.execute(
        select(
            MaintenanceCollectionEvidence,
            BusinessFile,
        )
        .join(
            BusinessFile,
            BusinessFile.file_id == MaintenanceCollectionEvidence.file_id,
        )
        .where(MaintenanceCollectionEvidence.milestone_id == milestone_id)
        .order_by(MaintenanceCollectionEvidence.uploaded_at.desc())
    ).all()
    return [
        {
            "evidence_id": evidence.evidence_id,
            "file_id": file_row.file_id,
            "md5": evidence.md5,
            "sha256": file_row.sha256,
            "original_filename": file_row.original_filename,
            "mime_type": file_row.mime_type,
            "size_bytes": file_row.size_bytes,
            "uploaded_by": evidence.uploaded_by,
            "uploaded_at": evidence.uploaded_at.isoformat(),
            "is_active": evidence.is_active,
        }
        for evidence, file_row in rows
    ]


def save_evidence(
    db: Session,
    *,
    milestone_id: str,
    operator: str,
    filename: str | None,
    mime_type: str | None,
    content: bytes,
) -> dict | None:
    """上传回款提醒凭证。节点不存在 → None；同里程碑同 md5 重放 → 返回既有记录。"""
    milestone = db.scalar(
        select(MaintenanceCollectionMilestone.milestone_id).where(
            MaintenanceCollectionMilestone.milestone_id == milestone_id
        )
    )
    if milestone is None:
        return None
    safe_name, extension, safe_mime = validate_attachment(
        filename=filename,
        mime_type=mime_type,
        content=content,
    )
    md5_digest = hashlib.md5(content).hexdigest()
    sha256_digest = hashlib.sha256(content).hexdigest()
    existing = db.execute(
        select(MaintenanceCollectionEvidence)
        .where(
            MaintenanceCollectionEvidence.milestone_id == milestone_id,
            MaintenanceCollectionEvidence.md5 == md5_digest,
            MaintenanceCollectionEvidence.is_active.is_(True),
        )
        .order_by(MaintenanceCollectionEvidence.uploaded_at.desc())
    ).scalars().first()
    if existing is not None:
        return {
            "evidence_id": existing.evidence_id,
            "file_id": existing.file_id,
            "milestone_id": milestone_id,
            "md5": md5_digest,
            "replayed": True,
        }
    file_id = str(uuid4())
    object_key = f"collection_evidence/{file_id[:2]}/{file_id}{extension}"
    path = _resolved_path(object_key)
    file_row = BusinessFile(
        file_id=file_id,
        storage_provider="local",
        object_key=object_key,
        original_filename=safe_name,
        mime_type=safe_mime,
        size_bytes=len(content),
        sha256=sha256_digest,
        security_state="active",
        uploaded_by=operator,
        version=1,
    )
    evidence_row = MaintenanceCollectionEvidence(
        evidence_id=str(uuid4()),
        milestone_id=milestone_id,
        file_id=file_id,
        md5=md5_digest,
        uploaded_by=operator[:64],
    )
    db.add(file_row)
    db.flush()
    db.add(evidence_row)
    try:
        db.flush()
    except IntegrityError:
        # 并发同 md5：DB 部分唯一索引兜底，回滚本笔后稳定重放既有凭证（round-5 Blocker 5）
        db.rollback()
        existing = db.execute(
            select(MaintenanceCollectionEvidence)
            .where(
                MaintenanceCollectionEvidence.milestone_id == milestone_id,
                MaintenanceCollectionEvidence.md5 == md5_digest,
                MaintenanceCollectionEvidence.is_active.is_(True),
            )
        ).scalars().first()
        if existing is None:
            raise
        return {
            "evidence_id": existing.evidence_id,
            "file_id": existing.file_id,
            "milestone_id": milestone_id,
            "md5": md5_digest,
            "replayed": True,
        }
    # 文件落盘推迟到 API 的 DB commit 之后（round-6 Blocker 6）：
    # DB 行先定案，文件失败可把凭证置 inactive 补偿，不产生指向缺失文件的活跃行。
    return {
        "evidence_id": evidence_row.evidence_id,
        "file_id": file_id,
        "object_key": object_key,
        "milestone_id": milestone_id,
        "md5": md5_digest,
        "sha256": sha256_digest,
        "size_bytes": len(content),
        "original_filename": safe_name,
        "mime_type": safe_mime,
        "replayed": False,
    }


def evidence_paths(file_id: str, object_key: str) -> tuple[Path, Path]:
    """凭证数据文件与 yml sidecar 的落盘路径（异常补偿清理用）。"""
    data_path = _resolved_path(object_key)
    meta_path = _root() / file_id[:2] / f"{file_id}.yml"
    return data_path, meta_path


def write_evidence_files(
    *, file_id: str, object_key: str, content: bytes, meta: dict
) -> None:
    """DB 已 commit 后落盘凭证（binary → yml；任一步失败补偿清理，不留半套文件）。"""
    path = _resolved_path(object_key)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temp_name = tempfile.mkstemp(prefix=".upload-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise
    try:
        _write_meta_sidecar(file_id, meta)
    except BaseException:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        raise


def try_close_milestone_after_upload(
    db: Session,
    *,
    milestone_id: str,
    evidence_id: str,
    operator: str,
    user_ctx,
    as_of,
) -> dict:
    """上传凭证 = 回款提醒关闭（§2.1）：上传成功后同事务标记 handled。

    与 follow_up 写路径共用全部门（canary/实名/项目范围/状态机/幂等账本）；
    节点状态不允许关闭（incomplete/needs_review/已处理）时只报告不抛错。
    """
    from app.services import maintenance_collection_reminders as reminders

    milestone = db.get(MaintenanceCollectionMilestone, milestone_id)
    if milestone is None:
        return {"closed": False, "reason": "节点不存在"}
    if milestone.follow_up_status == "handled":
        # 已关闭（本凭证或他处关闭）：重放/重复上传直接报告关闭，不触发幂等冲突
        return {"closed": True, "reason": None}
    try:
        payload = reminders.follow_up_collection_milestone(
            db,
            milestone_id=milestone_id,
            expected_version=milestone.version,
            idempotency_key=f"evidence-close:{evidence_id}",
            action="handle",
            planned_month=None,
            note="上传凭证自动关闭",
            reason=None,
            operator=operator,
            user_ctx=user_ctx,
            as_of=as_of,
        )
    except reminders.CollectionReminderConflict:
        # 并发关闭冲突：以节点实际状态为准
        db.refresh(milestone)
        return {
            "closed": milestone.follow_up_status == "handled",
            "reason": "并发关闭冲突，以节点状态为准",
        }
    except reminders.CollectionReminderInvalid as exc:
        return {"closed": False, "reason": str(exc)}
    if payload is None:
        return {"closed": False, "reason": "节点不存在"}
    return {"closed": True, "follow_up_row": payload["row"]}
