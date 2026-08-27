"""回款提醒凭证（F6）：上传 → 独立目录文件 + yml sidecar + DB 只记 md5。

复用 business_file 元数据层（sha256/大小/MIME/上传人）与验收附件校验
（validate_attachment：扩展名/MIME/内容一致性、20MB 上限、PDF 脚本剥离检测）。
"""
from __future__ import annotations

import fcntl
import hashlib
import os
import tempfile
from contextlib import contextmanager
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


def _active_evidence_payload(
    db: Session,
    *,
    milestone_id: str,
    md5_digest: str,
    sha256_digest: str,
    size_bytes: int,
) -> dict | None:
    """Return a replayable active row, refusing an MD5-only collision.

    The database uniqueness key is the historical ``(milestone_id, md5)``
    index.  SHA-256 and byte length are therefore rechecked before an upload is
    treated as an idempotent replay.  The full storage projection is returned
    so the API can verify/repair the files *before* closing the milestone.
    """
    pair = db.execute(
        select(MaintenanceCollectionEvidence, BusinessFile)
        .join(BusinessFile, BusinessFile.file_id == MaintenanceCollectionEvidence.file_id)
        .where(
            MaintenanceCollectionEvidence.milestone_id == milestone_id,
            MaintenanceCollectionEvidence.md5 == md5_digest,
            MaintenanceCollectionEvidence.is_active.is_(True),
        )
        .order_by(MaintenanceCollectionEvidence.uploaded_at.desc())
    ).first()
    if pair is None:
        return None
    evidence, file_row = pair
    if file_row.sha256 != sha256_digest or int(file_row.size_bytes) != size_bytes:
        raise CollectionEvidenceError(
            "凭证 MD5 与既有文件冲突，SHA-256 或文件大小不一致，已拒绝重放"
        )
    return {
        "evidence_id": evidence.evidence_id,
        "file_id": file_row.file_id,
        "object_key": file_row.object_key,
        "milestone_id": milestone_id,
        "md5": md5_digest,
        "sha256": file_row.sha256,
        "size_bytes": int(file_row.size_bytes),
        "original_filename": file_row.original_filename,
        "mime_type": file_row.mime_type,
        "uploaded_by": evidence.uploaded_by,
        "uploaded_at": evidence.uploaded_at.isoformat(),
        "replayed": True,
    }


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


@contextmanager
def _evidence_write_lock(file_id: str, directory: Path):
    """Serialize repair/replay writes for one evidence object across workers.

    The lock file is intentionally retained.  Removing a flock file after
    unlock allows a waiter on the old inode and a new opener on a replacement
    inode to enter concurrently.
    """
    lock_path = directory / f".{file_id}.lock"
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _restore_file_snapshot(*, target: Path, backup: Path, existed: bool) -> None:
    """Restore the pre-write inode, or remove only this locked new object."""
    if existed:
        # POSIX rename is a no-op when both names already reference the same
        # inode; explicitly drop the backup name in that case.
        if target.exists() and os.path.samefile(backup, target):
            os.unlink(backup)
            return
        os.replace(backup, target)
        return
    try:
        os.unlink(target)
    except FileNotFoundError:
        pass


def _discard_file_snapshot(backup: Path) -> None:
    try:
        os.unlink(backup)
    except FileNotFoundError:
        pass


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
    existing = _active_evidence_payload(
        db,
        milestone_id=milestone_id,
        md5_digest=md5_digest,
        sha256_digest=sha256_digest,
        size_bytes=len(content),
    )
    if existing is not None:
        return existing
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
        existing = _active_evidence_payload(
            db,
            milestone_id=milestone_id,
            md5_digest=md5_digest,
            sha256_digest=sha256_digest,
            size_bytes=len(content),
        )
        if existing is None:
            raise
        return existing
    # API 必须先把 binary + sidecar 原子落盘，再关闭提醒并提交本行。这样文件
    # 失败时整笔数据库事务回滚，不会留下“提醒已关闭但没有有效凭证”的状态。
    # 若最终 DB commit 的确认丢失，磁盘上最多留下不可见孤儿文件；不能反向删除，
    # 否则可能把实际上已经提交成功的凭证删掉。
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
        "uploaded_by": evidence_row.uploaded_by,
        "uploaded_at": evidence_row.uploaded_at.isoformat(),
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
    """DB commit 前写入 binary + sidecar；失败恢复已有完整文件对。

    A replay writes the same ``object_key`` as the active database row.  Each
    individual rename is atomic, but the two renames are not one filesystem
    transaction.  A per-object cross-process lock plus hard-link snapshots
    therefore preserve the old pair until both replacements have succeeded.
    """
    path = _resolved_path(object_key)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    meta_path = _root() / file_id[:2] / f"{file_id}.yml"
    meta_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    data_backup = path.parent / f".{file_id}.data.rollback"
    meta_backup = meta_path.parent / f".{file_id}.meta.rollback"

    with _evidence_write_lock(file_id, path.parent):
        # A killed worker may leave a rollback hard link behind.  Restoring it
        # before taking a new snapshot gives the next replay a known-good base.
        if data_backup.exists():
            _restore_file_snapshot(
                target=path,
                backup=data_backup,
                existed=True,
            )
        if meta_backup.exists():
            _restore_file_snapshot(
                target=meta_path,
                backup=meta_backup,
                existed=True,
            )

        data_existed = path.exists()
        meta_existed = meta_path.exists()
        try:
            if data_existed:
                os.link(path, data_backup)
            if meta_existed:
                os.link(meta_path, meta_backup)
        except BaseException:
            _discard_file_snapshot(data_backup)
            _discard_file_snapshot(meta_backup)
            raise

        descriptor, temp_name = tempfile.mkstemp(prefix=".upload-", dir=path.parent)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temp_name, 0o600)
            os.replace(temp_name, path)
            _write_meta_sidecar(file_id, meta)
        except BaseException as exc:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass
            restore_errors: list[BaseException] = []
            for target, backup, existed in (
                (path, data_backup, data_existed),
                (meta_path, meta_backup, meta_existed),
            ):
                try:
                    _restore_file_snapshot(
                        target=target,
                        backup=backup,
                        existed=existed,
                    )
                except BaseException as restore_exc:  # pragma: no cover - fatal I/O
                    restore_errors.append(restore_exc)
            if restore_errors:
                raise CollectionEvidenceError(
                    "凭证写入失败，且旧文件恢复失败，需要人工检查存储目录"
                ) from exc
            raise
        else:
            _discard_file_snapshot(data_backup)
            _discard_file_snapshot(meta_backup)


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
