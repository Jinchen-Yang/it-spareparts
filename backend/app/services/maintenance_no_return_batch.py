"""领用行批量应返/免返（ITSP-2，D-31 第 5 款）。

口径依据：docs/decisions/0009 D-31 第 5 款（2026-10-15 拍板）：
- 批量作用于领用行，支持 no_return=True/False/None（免返/应返/恢复按规则判断）；
- 页面批量仍逐行编排，不承诺跨行数据库事务——本服务按领用单逐单独立
  事务，单内失败回滚该单、其余照常，逐单上报结果；
- 写入复用领用事实编辑原语 apply_locked_site_issue_edits（不另写赋值
  算法），审计/版本/校验由原语负责；
- 发货关联领用行按默认协议拒绝并逐单上报（须走原有更正流程）；
- 免返文本框仍只提醒，不由文本生成批量命令（D-32，本服务不接收 note）。
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.services.maintenance_project_operations import (
    MaintenanceSiteIssue,
    MaintenanceSiteIssueLine,
    _lock_project_for_fact_write,
)
from app.services.maintenance_site_issue_facts import (
    SiteIssueEditError,
    apply_locked_site_issue_edits,
)


class BatchNoReturnError(ValueError):
    """整单级参数错误（422，零写入）。"""


@dataclass
class BatchNoReturnIssueResult:
    issue_id: str
    line_ids: list[str]
    ok: bool
    changed: bool = False
    error: str | None = None


def apply_batch_no_return(
    db: Session, *, project_id: str, line_ids: list[str],
    no_return: bool | None, reason: str, operated_by: str,
) -> dict:
    """按领用单分组逐单设置 no_return，逐单独立事务，逐单上报。

    调用方（API 层）已做项目访问鉴权；本函数负责行归属、锁序与逐单事务。
    """
    if no_return is not True and no_return is not False and no_return is not None:
        raise BatchNoReturnError(
            "no_return 只允许 true（免返）/ false（应返）/ null（恢复按规则判断）")
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 256:
        raise BatchNoReturnError("必须填写批量操作原因（1–256 字符）")
    if not isinstance(operated_by, str) or not operated_by.strip() or len(operated_by) > 64:
        raise BatchNoReturnError("缺少实名操作者")
    clean_ids = [lid for lid in (x.strip() for x in line_ids) if lid]
    if not clean_ids:
        raise BatchNoReturnError("至少选择一条领用明细行")
    if len(set(clean_ids)) != len(clean_ids):
        raise BatchNoReturnError("领用明细行存在重复")

    rows = db.execute(
        select(MaintenanceSiteIssueLine).where(
            MaintenanceSiteIssueLine.issue_line_id.in_(clean_ids))
    ).scalars().all()
    by_id = {row.issue_line_id: row for row in rows}
    missing = [lid for lid in clean_ids if lid not in by_id]
    if missing:
        raise BatchNoReturnError(f"领用明细行不存在：{'、'.join(missing[:5])}")

    issue_ids = list(dict.fromkeys(by_id[lid].issue_id for lid in clean_ids))
    issue_rows = db.execute(
        select(MaintenanceSiteIssue).where(
            MaintenanceSiteIssue.issue_id.in_(issue_ids))
    ).scalars().all()
    issue_project = {row.issue_id: row.project_id for row in issue_rows}
    foreign = [lid for lid in clean_ids
               if issue_project.get(by_id[lid].issue_id) != project_id]
    if foreign:
        raise BatchNoReturnError("存在不属于当前项目的领用明细行")

    # 与既有领用写路径同锁序：项目 → 领用单 → 全部活跃明细行
    locked_project = _lock_project_for_fact_write(db, project_id)
    if locked_project is None or not locked_project.is_active:
        raise BatchNoReturnError("项目主档已归档或不存在")

    results: list[BatchNoReturnIssueResult] = []
    changed_lines = 0
    for issue_id in issue_ids:
        issue_line_ids = [lid for lid in clean_ids
                          if by_id[lid].issue_id == issue_id]
        issue = db.scalar(
            select(MaintenanceSiteIssue)
            .where(MaintenanceSiteIssue.issue_id == issue_id)
            .with_for_update()
        )
        if issue is None:
            results.append(BatchNoReturnIssueResult(issue_id, issue_line_ids,
                                                    False, error="领用单不存在"))
            continue
        # 默认协议要求调用方持有该单全部活跃明细行的锁
        db.scalars(
            select(MaintenanceSiteIssueLine)
            .where(MaintenanceSiteIssueLine.issue_id == issue_id,
                   MaintenanceSiteIssueLine.is_active.is_(True))
            .with_for_update()
        ).all()
        try:
            edited = apply_locked_site_issue_edits(
                db, project=locked_project, issue=issue,
                line_changes={lid: {"no_return": no_return}
                              for lid in issue_line_ids},
                header_changes=None,
                operated_by=operated_by, reason=reason,
            )
            db.commit()
        except SiteIssueEditError as exc:
            db.rollback()
            results.append(BatchNoReturnIssueResult(
                issue_id, issue_line_ids, ok=False,
                error=getattr(exc, "message", None) or str(exc)))
            continue
        results.append(BatchNoReturnIssueResult(
            issue_id, issue_line_ids, ok=True, changed=bool(edited.changed)))
        if edited.changed:
            changed_lines += len(issue_line_ids)
    return {
        "results": [
            {"issue_id": r.issue_id, "line_ids": r.line_ids, "ok": r.ok,
             "changed": r.changed, "error": r.error}
            for r in results
        ],
        "issue_total": len(results),
        "issue_ok": sum(1 for r in results if r.ok),
        "line_total": len(clean_ids),
        "changed_lines": changed_lines,
    }
