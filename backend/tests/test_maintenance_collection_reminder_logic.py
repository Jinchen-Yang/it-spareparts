"""车道 A 纯状态派生红测（Task 2 Step 2.1）。

覆盖：
- reminder_state 派生优先级 ``needs_review > handled > incomplete > overdue >
  due_this_month > upcoming``（设计 §4.3）。
- month 精度只比较自然月 YYYY-MM；day 精度按具体日期。
- ``as_of`` 必须显式传入，禁止隐式系统日期。
- ``select_next_actionable_milestone`` 不选择普通 handled 节点，按
  ``needs_review > overdue > due_this_month > incomplete > upcoming``、
  再按计划月份和期次确定。
- follow-up payload_hash 固定包含实名 actor、路径 milestone 与规范化 body。
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from app.services.maintenance_collection_reminders import (
    derive_reminder_state,
    follow_up_payload_hash,
    select_next_actionable_milestone,
)


def _milestone(**overrides) -> SimpleNamespace:
    """合成节点：与 ORM 字段同名，便于纯函数测试。"""
    base = {
        "milestone_id": "m-default",
        "project_id": "p-default",
        "project_contract_id": "pc-default",
        "sequence": 1,
        "planned_date": date(2026, 9, 1),
        "date_precision": "month",
        "planned_amount": "18000.00",
        "completeness_state": "complete",
        "follow_up_status": "pending",
        "follow_up_review_required": False,
        "follow_up_note": None,
        "followed_up_by": None,
        "followed_up_at": None,
        "version": 1,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


# ---------- 派生优先级 ----------

def test_derive_needs_review_beats_handled_and_everything_else():
    milestone = _milestone(
        follow_up_review_required=True,
        follow_up_status="handled",
        planned_date=date(2020, 1, 1),
    )
    assert derive_reminder_state(milestone, as_of=date(2026, 8, 14)) == "needs_review"


def test_derive_handled_beats_time_states_but_loses_to_needs_review():
    handled = _milestone(follow_up_status="handled", planned_date=date(2020, 1, 1))
    assert derive_reminder_state(handled, as_of=date(2026, 8, 14)) == "handled"
    review = _milestone(
        follow_up_status="handled",
        follow_up_review_required=True,
        planned_date=date(2026, 8, 1),
    )
    assert derive_reminder_state(review, as_of=date(2026, 8, 14)) == "needs_review"


def test_derive_incomplete_beats_time_states():
    incomplete = _milestone(
        planned_date=None,
        planned_amount=None,
        completeness_state="date_only",
    )
    assert derive_reminder_state(incomplete, as_of=date(2026, 8, 14)) == "incomplete"


def test_derive_overdue_beats_due_this_month_beats_upcoming():
    overdue = _milestone(planned_date=date(2026, 6, 1))
    due = _milestone(planned_date=date(2026, 8, 15))
    upcoming = _milestone(planned_date=date(2026, 9, 1))
    as_of = date(2026, 8, 14)
    assert derive_reminder_state(overdue, as_of=as_of) == "overdue"
    assert derive_reminder_state(due, as_of=as_of) == "due_this_month"
    assert derive_reminder_state(upcoming, as_of=as_of) == "upcoming"


# ---------- month 精度：只比较 YYYY-MM ----------

def test_derive_month_precision_only_compares_natural_month():
    as_of = date(2026, 8, 14)
    # 当月 31 日（月内未来日期）→ 本月跟进；早于当月起点的同月日期 → 逾期。
    assert (
        derive_reminder_state(
            _milestone(planned_date=date(2026, 8, 31), date_precision="month"),
            as_of=as_of,
        )
        == "due_this_month"
    )
    assert (
        derive_reminder_state(
            _milestone(planned_date=date(2026, 8, 1), date_precision="month"),
            as_of=as_of,
        )
        == "due_this_month"
    )
    assert (
        derive_reminder_state(
            _milestone(planned_date=date(2026, 7, 31), date_precision="month"),
            as_of=as_of,
        )
        == "overdue"
    )
    assert (
        derive_reminder_state(
            _milestone(planned_date=date(2026, 9, 1), date_precision="month"),
            as_of=as_of,
        )
        == "upcoming"
    )


# ---------- day 精度 ----------

def test_derive_day_precision_overdue_due_this_month_upcoming():
    as_of = date(2026, 8, 14)
    assert (
        derive_reminder_state(
            _milestone(planned_date=date(2026, 8, 13), date_precision="day"),
            as_of=as_of,
        )
        == "overdue"
    )
    assert (
        derive_reminder_state(
            _milestone(planned_date=date(2026, 8, 14), date_precision="day"),
            as_of=as_of,
        )
        == "due_this_month"
    )
    assert (
        derive_reminder_state(
            _milestone(planned_date=date(2026, 8, 31), date_precision="day"),
            as_of=as_of,
        )
        == "due_this_month"
    )
    assert (
        derive_reminder_state(
            _milestone(planned_date=date(2026, 9, 1), date_precision="day"),
            as_of=as_of,
        )
        == "upcoming"
    )


# ---------- 显式 as_of ----------

def test_derive_requires_explicit_as_of_and_is_deterministic():
    milestone = _milestone(planned_date=date(2026, 8, 20), date_precision="day")
    assert derive_reminder_state(milestone, as_of=date(2026, 8, 14)) == "due_this_month"
    assert derive_reminder_state(milestone, as_of=date(2026, 8, 20)) == "due_this_month"
    assert derive_reminder_state(milestone, as_of=date(2026, 8, 21)) == "overdue"
    assert derive_reminder_state(milestone, as_of=date(2026, 7, 31)) == "upcoming"
    assert derive_reminder_state(milestone, as_of=date(2026, 9, 1)) == "overdue"


# ---------- 下一条可跟进节点 ----------

def test_next_actionable_never_selects_plain_handled():
    handled = _milestone(
        milestone_id="handled-1",
        follow_up_status="handled",
        planned_date=date(2026, 1, 1),
    )
    upcoming = _milestone(milestone_id="upcoming-1", planned_date=date(2026, 10, 1))
    best = select_next_actionable_milestone([handled, upcoming], as_of=date(2026, 8, 14))
    assert best is not None
    assert best.milestone_id == "upcoming-1"


def test_next_actionable_returns_none_when_only_handled():
    handled = _milestone(follow_up_status="handled")
    assert (
        select_next_actionable_milestone([handled], as_of=date(2026, 8, 14))
        is None
    )
    assert select_next_actionable_milestone([], as_of=date(2026, 8, 14)) is None


def test_next_actionable_priority_order():
    as_of = date(2026, 8, 14)
    upcoming = _milestone(milestone_id="upcoming", planned_date=date(2026, 12, 1))
    incomplete = _milestone(
        milestone_id="incomplete",
        planned_date=None,
        planned_amount=None,
        completeness_state="date_only",
    )
    due = _milestone(milestone_id="due", planned_date=date(2026, 8, 20))
    overdue = _milestone(milestone_id="overdue", planned_date=date(2026, 5, 1))
    needs_review = _milestone(
        milestone_id="needs-review",
        follow_up_status="handled",
        follow_up_review_required=True,
        planned_date=date(2026, 7, 1),
    )
    # needs_review > overdue > due_this_month > incomplete > upcoming
    assert select_next_actionable_milestone(
        [upcoming, incomplete, due, overdue, needs_review], as_of=as_of
    ).milestone_id == "needs-review"
    assert select_next_actionable_milestone(
        [upcoming, incomplete, due, overdue], as_of=as_of
    ).milestone_id == "overdue"
    assert select_next_actionable_milestone(
        [upcoming, incomplete, due], as_of=as_of
    ).milestone_id == "due"
    assert select_next_actionable_milestone(
        [upcoming, incomplete], as_of=as_of
    ).milestone_id == "incomplete"
    assert (
        select_next_actionable_milestone([upcoming], as_of=as_of).milestone_id
        == "upcoming"
    )


def test_next_actionable_tie_break_by_planned_month_then_sequence():
    as_of = date(2026, 8, 14)
    later = _milestone(
        milestone_id="later",
        sequence=2,
        planned_date=date(2026, 10, 1),
    )
    earlier = _milestone(
        milestone_id="earlier",
        sequence=1,
        planned_date=date(2026, 9, 1),
    )
    same_month_first_seq = _milestone(
        milestone_id="same-month-seq-1",
        sequence=1,
        planned_date=date(2026, 9, 1),
    )
    same_month_second_seq = _milestone(
        milestone_id="same-month-seq-2",
        sequence=2,
        planned_date=date(2026, 9, 1),
    )
    assert (
        select_next_actionable_milestone([later, earlier], as_of=as_of).milestone_id
        == "earlier"
    )
    assert (
        select_next_actionable_milestone(
            [same_month_second_seq, same_month_first_seq], as_of=as_of
        ).milestone_id
        == "same-month-seq-1"
    )


# ---------- payload_hash ----------

def test_follow_up_payload_hash_covers_actor_path_and_body():
    base = dict(
        actor_user_id=7,
        milestone_id="m-1",
        expected_version=1,
        action="handle",
        planned_month=None,
        note="跟进完毕",
        reason=None,
    )
    assert follow_up_payload_hash(**base) == follow_up_payload_hash(**base)
    assert follow_up_payload_hash(**base) != follow_up_payload_hash(
        **{**base, "actor_user_id": 8}
    )
    assert follow_up_payload_hash(**base) != follow_up_payload_hash(
        **{**base, "milestone_id": "m-2"}
    )
    assert follow_up_payload_hash(**base) != follow_up_payload_hash(
        **{**base, "note": "不同备注"}
    )
    assert follow_up_payload_hash(**base) != follow_up_payload_hash(
        **{**base, "expected_version": 2}
    )
