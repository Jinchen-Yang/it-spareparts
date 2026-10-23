"""Final site-issue identity validation shared by parsing and application."""
from __future__ import annotations

from collections import defaultdict
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.maintenance_project_operations import MaintenanceSiteIssue
from app.services.maintenance_workbook_plans import SiteReturnFlag, WorkbookError


def _v2_duplicate_issue_no(issue_no: str, flags: list[SiteReturnFlag]) -> WorkbookError:
    rows = sorted({flag.row_no for flag in flags if flag.row_no is not None})
    location = f"第 {'、'.join(map(str, rows))} 行" if rows else "本次上传"
    return WorkbookError(
        "duplicate_issue_no",
        f"06_领用返还 {location}的领用单号 {issue_no} 与本项目其他领用单重复，"
        "同一项目内两张不同领用单的单号不能重复，请修改其中一张后重试",
        details={"rows": [
            {"row_no": flag.row_no, "issue_id": flag.issue_id, "issue_no": issue_no}
            for flag in sorted(flags, key=lambda item: item.row_no or 0)
            if flag.row_no is not None
        ]},
    )


def _v2_check_site_issue_numbers(
    db: Session, project_id: str, flags: list[SiteReturnFlag] | tuple[SiteReturnFlag, ...],
) -> dict[str, tuple[str, date]]:
    """收齐最终单头；既有同号交给合单计划，新建身份仍不能隐式合并。"""
    issues = {issue.issue_id: issue for issue in db.scalars(
        select(MaintenanceSiteIssue).where(MaintenanceSiteIssue.project_id == project_id)
    )}
    headers: dict[str, tuple[str, date]] = {}
    for flag in flags:
        if flag.is_void or flag.issue_id is None:
            continue
        if flag.issue_id not in issues:
            raise WorkbookError("project_mismatch", "领用单归属已变化，请重新下载")
        # 新明细的日期不是改头意图，附加到本单的最终单头即可。
        if flag.is_create or flag.issue_no is None or flag.issue_date is None:
            continue
        requested = (flag.issue_no, flag.issue_date)
        if headers.setdefault(flag.issue_id, requested) != requested:
            raise WorkbookError(
                "conflicting_issue_header", "同一领用单的多行包含不同单号或日期，本次未写入",
            )
    owners: dict[str, set[tuple[str, str]]] = defaultdict(set)
    for issue_id, issue in issues.items():
        final_no = headers.get(issue_id, (issue.issue_no, issue.issue_date))[0]
        owners[final_no].add(("existing", issue_id))
    flags_by_no: dict[str, list[SiteReturnFlag]] = defaultdict(list)
    for flag in flags:
        if flag.is_void or not flag.issue_no:
            continue
        flags_by_no[flag.issue_no].append(flag)
        if flag.is_create:
            if flag.issue_id is None:
                # 同号新明细是一张新单；自动生成的新头不能隐式并入既有单。
                owners[flag.issue_no].add(("new", flag.issue_no))
            else:
                issue = issues[flag.issue_id]
                final_no = headers.get(flag.issue_id, (issue.issue_no, issue.issue_date))[0]
                if flag.issue_no != final_no:
                    raise _v2_duplicate_issue_no(flag.issue_no, flags_by_no[flag.issue_no])
    for issue_no, identities in owners.items():
        if len(identities) > 1 and any(kind == "new" for kind, _identity in identities):
            raise _v2_duplicate_issue_no(issue_no, flags_by_no[issue_no])
    return headers
