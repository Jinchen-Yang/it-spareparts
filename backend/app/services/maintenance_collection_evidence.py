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
    db.flush()
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
    _write_meta_sidecar(
        file_id,
        {
            "file_id": file_id,
            "milestone_id": milestone_id,
            "original_filename": safe_name,
            "mime_type": safe_mime,
            "size_bytes": len(content),
            "md5": md5_digest,
            "sha256": sha256_digest,
            "uploaded_by": operator,
            "uploaded_at": datetime.now(timezone.utc).isoformat(),
            "storage": "local",
        },
    )
    return {
        "evidence_id": evidence_row.evidence_id,
        "file_id": file_id,
        "milestone_id": milestone_id,
        "md5": md5_digest,
        "sha256": sha256_digest,
        "size_bytes": len(content),
        "original_filename": safe_name,
        "mime_type": safe_mime,
        "replayed": False,
    }
