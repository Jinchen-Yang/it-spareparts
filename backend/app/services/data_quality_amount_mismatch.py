"""DEV-05C1：把 ETL 的确定性金额提示转成正式数据疑点。"""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal

from sqlalchemy import Integer, String, any_, bindparam, select
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Session

from app.etl import anomaly, mapping
from app.models.data_quality import FactDataQualityIssue
from app.models.purchase import FPurchaseLine
from app.models.sales import FSalesLine
from app.services import data_quality

RULE_CODE = "amount_mismatch"
RULE_VERSION = "etl-v1"
SYSTEM_DETECTOR = "system:etl-import"


def _decimal_text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    if value == 0:
        return "0"
    return format(value.normalize(), "f")


def _source_payload(side: str, line) -> dict:
    return {
        "side": side,
        "raw_line_id": line.raw_line_id,
        "part_id": line.part_id,
        "pn_std": line.pn_std,
        "unit": line.unit,
        "qty": _decimal_text(line.qty),
        "unit_price": _decimal_text(line.unit_price),
        "line_amount": _decimal_text(line.line_amount),
    }


def _fingerprint(side: str, line) -> str:
    raw = json.dumps(
        _source_payload(side, line), ensure_ascii=False, sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _evidence(
    line, *, current_match: bool = True, detection_source: str = "etl_import",
) -> dict:
    expected = (
        line.qty * line.unit_price
        if line.qty is not None and line.unit_price is not None else None
    )
    difference = (
        abs(line.line_amount - expected)
        if line.line_amount is not None and expected is not None else None
    )
    return {
        "qty": _decimal_text(line.qty),
        "unit_price": _decimal_text(line.unit_price),
        "line_amount": _decimal_text(line.line_amount),
        "expected_amount": _decimal_text(expected),
        "absolute_difference": _decimal_text(difference),
        "tolerance": _decimal_text(anomaly.AMOUNT_TOL),
        "current_match": current_match,
        "detection_source": detection_source,
    }


def detect_imported_lines(
    db: Session, *, file_type: str, raw_line_ids: list[str],
    detected_by: str | None,
) -> dict:
    """检测一次导入实际触及的当前事实行；始终加入调用方事务。"""
    empty = {
        "scanned": 0, "matched": 0, "created": 0,
        "refreshed": 0, "unchanged": 0, "source_changed": 0,
    }
    if file_type not in {mapping.PURCHASE, mapping.SALES} or not raw_line_ids:
        return empty

    side = "purchase" if file_type == mapping.PURCHASE else "sales"
    model = FPurchaseLine if side == "purchase" else FSalesLine
    unique_raw_ids = sorted(set(raw_line_ids))
    rows = db.scalars(
        select(model).where(
            model.raw_line_id == any_(bindparam(
                "detector_raw_line_ids", type_=ARRAY(String()),
            ))
        ).order_by(model.id),
        {"detector_raw_line_ids": unique_raw_ids},
    ).all()
    stats = {**empty, "scanned": len(rows)}
    actor = (detected_by or "").strip() or SYSTEM_DETECTOR
    if not rows:
        return stats
    issues = db.scalars(
        select(FactDataQualityIssue).where(
            FactDataQualityIssue.side == side,
            FactDataQualityIssue.rule_code == RULE_CODE,
            FactDataQualityIssue.line_id == any_(bindparam(
                "detector_line_ids", type_=ARRAY(Integer()),
            )),
        ).with_for_update(),
        {"detector_line_ids": [line.id for line in rows]},
    ).all()
    issue_by_line_id = {issue.line_id: issue for issue in issues}

    for line in rows:
        matched = RULE_CODE in anomaly.line_flags(
            line.qty, line.unit_price, line.line_amount,
        )
        existing = issue_by_line_id.get(line.id)
        if not matched:
            if existing is None:
                continue
            evidence = _evidence(line, current_match=False)
            fingerprint = _fingerprint(side, line)
            if data_quality.detection_issue_is_current(
                existing, line, rule_version=RULE_VERSION,
                evidence=evidence, source_fingerprint=fingerprint,
                expected_status="source_changed",
            ):
                stats["unchanged"] += 1
                continue
            prior_version = existing.version
            result = data_quality.mark_issue_source_changed(
                db, side=side, line_id=line.id, rule_code=RULE_CODE,
                rule_version=RULE_VERSION,
                evidence=evidence,
                source_fingerprint=fingerprint, detected_by=actor,
            )
            if result is not None and result["version"] != prior_version:
                stats["source_changed"] += 1
            else:
                stats["unchanged"] += 1
            continue
        stats["matched"] += 1
        prior_version = existing.version if existing is not None else None
        evidence = _evidence(line)
        fingerprint = _fingerprint(side, line)
        if existing is not None and data_quality.detection_issue_is_current(
            existing, line, rule_version=RULE_VERSION,
            evidence=evidence, source_fingerprint=fingerprint,
        ):
            stats["unchanged"] += 1
            continue
        result = data_quality.create_or_refresh_issue(
            db, side=side, line_id=line.id, rule_code=RULE_CODE,
            rule_version=RULE_VERSION, evidence=evidence,
            source_fingerprint=fingerprint, detected_by=actor,
        )
        if existing is None:
            stats["created"] += 1
        elif result["version"] == prior_version:
            stats["unchanged"] += 1
        else:
            stats["refreshed"] += 1
            if result["status"] == "source_changed":
                stats["source_changed"] += 1
    return stats


def _preview_sample(side: str, line, *, action: str) -> dict:
    evidence = _evidence(
        line,
        current_match=(
            RULE_CODE in anomaly.line_flags(line.qty, line.unit_price, line.line_amount)
        ),
        detection_source="historical_scan",
    )
    return {
        "side": side,
        "line_id": line.id,
        "raw_line_id": line.raw_line_id,
        "part_id": line.part_id,
        "pn_std": line.pn_std,
        "action": action,
        **evidence,
    }


def preview_history(
    db: Session, *, side: str | None = None, sample_limit: int = 20,
) -> dict:
    """只读扫描存量事实；不创建、刷新或失效任何疑点。"""
    if side not in {None, "purchase", "sales"}:
        raise data_quality.DataQualityValidationError("side 只能是 purchase 或 sales")
    if sample_limit < 0 or sample_limit > 100:
        raise data_quality.DataQualityValidationError("sample_limit 必须在 0 到 100 之间")

    summary = {
        "scanned": 0,
        "matched": 0,
        "existing": 0,
        "would_create": 0,
        "would_refresh": 0,
        "existing_no_longer_matches": 0,
    }
    samples: list[dict] = []
    sides = [side] if side is not None else ["purchase", "sales"]
    for current_side in sides:
        model = FPurchaseLine if current_side == "purchase" else FSalesLine
        existing = {
            issue.line_id: issue
            for issue in db.scalars(select(FactDataQualityIssue).where(
                FactDataQualityIssue.side == current_side,
                FactDataQualityIssue.rule_code == RULE_CODE,
            )).all()
        }
        lines = db.scalars(
            select(model).order_by(model.id).execution_options(yield_per=1000)
        )
        for line in lines:
            summary["scanned"] += 1
            matched = RULE_CODE in anomaly.line_flags(
                line.qty, line.unit_price, line.line_amount,
            )
            issue = existing.get(line.id)
            if matched:
                summary["matched"] += 1
                if issue is None:
                    action = "would_create"
                    summary["would_create"] += 1
                else:
                    summary["existing"] += 1
                    evidence = _evidence(line)
                    fingerprint = _fingerprint(current_side, line)
                    if any((
                        issue.part_id != line.part_id,
                        issue.rule_version != RULE_VERSION,
                        issue.evidence != evidence,
                        issue.source_fingerprint != fingerprint,
                    )):
                        action = "would_refresh"
                        summary["would_refresh"] += 1
                    else:
                        action = "unchanged"
                if len(samples) < sample_limit:
                    samples.append(_preview_sample(current_side, line, action=action))
            elif issue is not None:
                summary["existing_no_longer_matches"] += 1
                if len(samples) < sample_limit:
                    samples.append(_preview_sample(
                        current_side, line, action="would_mark_source_changed",
                    ))

    return {
        "dry_run": True,
        "rule_code": RULE_CODE,
        "rule_version": RULE_VERSION,
        "tolerance": _decimal_text(anomaly.AMOUNT_TOL),
        "side": side or "all",
        "sample_limit": sample_limit,
        "summary": summary,
        "samples": samples,
    }
