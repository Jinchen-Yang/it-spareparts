"""维保成本事实分层与预算决策门禁的单一真值测试。"""
import json
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import and_, event, func, select, text

from app import permissions, security
from app.agent import tools
from app.auth import hash_password
from app.etl import loader
from app.models.maintenance import FMaintenanceLine
from app.models.system import SysImportBatch, SysUser
from app.services import maintenance_cost, maintenance_workbook_renderer
from app.services import maintenance_cost_quality
from app.services.maintenance_cost import COSTED_SOURCES
from tests import factories as f


@pytest.mark.parametrize(
    ("source", "tax_basis", "amount", "confidence", "expected"),
    [
        ("direct", "inc", "1.00", "high", maintenance_cost_quality.COST_BUCKET_ACTUAL_INC),
        ("window", "ex", "1.00", "low", maintenance_cost_quality.COST_BUCKET_ACTUAL_EX),
        (
            "trace_avg",
            "inc",
            "1.00",
            "low",
            maintenance_cost_quality.COST_BUCKET_ESTIMATED_INC_LOW,
        ),
        (
            "sales_ref",
            "inc",
            "1.00",
            None,
            maintenance_cost_quality.COST_BUCKET_ESTIMATED_INC_OTHER,
        ),
        (
            "trace_avg",
            "ex",
            "1.00",
            "low",
            maintenance_cost_quality.COST_BUCKET_ESTIMATED_EX_LOW,
        ),
        (
            "sales_ref",
            "ex",
            "1.00",
            "medium",
            maintenance_cost_quality.COST_BUCKET_ESTIMATED_EX_OTHER,
        ),
        ("future", "inc", "1.00", "low", maintenance_cost_quality.COST_BUCKET_MISSING),
        ("direct", "gross", "1.00", "high", maintenance_cost_quality.COST_BUCKET_MISSING),
        ("direct", "inc", None, "high", maintenance_cost_quality.COST_BUCKET_MISSING),
        ("direct", "inc", "-1.00", "high", maintenance_cost_quality.COST_BUCKET_MISSING),
        ("direct", "inc", "NaN", "high", maintenance_cost_quality.COST_BUCKET_MISSING),
        ("direct", "inc", "Infinity", "high", maintenance_cost_quality.COST_BUCKET_MISSING),
    ],
)
def test_cost_bucket_constants_and_fail_closed_reverse_mapping(
    source,
    tax_basis,
    amount,
    confidence,
    expected,
):
    bucket = maintenance_cost_quality.cost_bucket(
        source,
        tax_basis,
        Decimal(amount) if amount is not None else None,
        confidence,
    )

    assert bucket == expected
    assert maintenance_cost_quality.bucket_tier(bucket) == (
        maintenance_cost_quality.source_tier(
            source,
            tax_basis,
            Decimal(amount) if amount is not None else None,
        )
    )
    assert maintenance_cost_quality.bucket_tier(999) == "missing"
    assert maintenance_cost_quality.bucket_tier(None) == "missing"


def test_generated_cost_bucket_sql_matches_python_for_full_input_matrix(db):
    """数据库真实生成表达式与 Python 真值全笛卡尔积等价，含特殊值。"""
    sources = [
        *sorted(maintenance_cost_quality.KNOWN_SOURCES),
        "none",
        "future_source",
        None,
    ]
    tax_bases = ["inc", "ex", "gross", None]
    amount_texts = [
        None,
        "-1",
        "0",
        "999999999999.99",
        "1000000000000",
        "NaN",
        "Infinity",
        "-Infinity",
    ]
    confidences = ["low", "other", None]
    samples = []
    expected = []
    for source in sources:
        for tax_basis in tax_bases:
            for amount_text in amount_texts:
                for confidence in confidences:
                    ordinal = len(samples)
                    samples.append({
                        "ordinal": ordinal,
                        "cost_source": source,
                        "cost_tax_basis": tax_basis,
                        "amount_text": amount_text,
                        "confidence": confidence,
                    })
                    expected.append(maintenance_cost_quality.cost_bucket(
                        source,
                        tax_basis,
                        Decimal(amount_text) if amount_text is not None else None,
                        confidence,
                    ))

    generation_expression = db.scalar(text(
        """
        SELECT generation_expression
        FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = 'f_maintenance_line'
          AND column_name = 'cost_bucket'
        """,
    ))
    assert generation_expression

    observed = db.execute(
        text(
            f"""
            WITH samples AS (
                SELECT
                    ordinal,
                    cost_source,
                    cost_tax_basis,
                    CAST(amount_text AS NUMERIC) AS cost_amount,
                    confidence
                FROM jsonb_to_recordset(CAST(:payload AS JSONB)) AS item(
                    ordinal INTEGER,
                    cost_source TEXT,
                    cost_tax_basis TEXT,
                    amount_text TEXT,
                    confidence TEXT
                )
            )
            SELECT ordinal, ({generation_expression}) AS cost_bucket
            FROM samples
            ORDER BY ordinal
            """,
        ),
        {"payload": json.dumps(samples)},
    ).all()

    assert [row.ordinal for row in observed] == list(range(len(samples)))
    assert [row.cost_bucket for row in observed] == expected


def test_cost_bucket_is_a_stored_generated_orm_column(db):
    column = FMaintenanceLine.__table__.c.cost_bucket
    assert column.computed is not None
    assert column.computed.persisted is True

    schema = db.execute(text(
        """
        SELECT is_generated, generation_expression, is_nullable
        FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = 'f_maintenance_line'
          AND column_name = 'cost_bucket'
        """,
    )).one()
    assert schema.is_generated == "ALWAYS"
    assert schema.is_nullable == "NO"
    assert "cost_amount" in schema.generation_expression
    assert "confidence" in schema.generation_expression


def test_cost_sources_are_classified_into_actual_estimated_or_missing():
    assert set(COSTED_SOURCES) == maintenance_cost_quality.KNOWN_SOURCES

    records = [
        ("direct", "inc", Decimal("100.00")),
        ("window", "ex", Decimal("20.00")),
        ("month_avg", "inc", Decimal("30.00")),
        ("trace_avg", "ex", Decimal("40.00")),
        ("sales_ref", "inc", Decimal("50.00")),
        ("none", None, None),
        (None, None, None),
        ("future_source", "inc", Decimal("999.00")),
        ("direct", "inc", None),
        ("window", "gross", Decimal("10.00")),
        ("direct", "inc", Decimal("-1.00")),
        ("direct", "ex", Decimal("0.00")),
        ("direct", "inc", Decimal("NaN")),
        ("direct", "inc", Decimal("Infinity")),
    ]

    summary = maintenance_cost_quality.summarize_records(records)

    assert summary == {
        "actual_cost_inc": Decimal("130.00"),
        "actual_cost_ex": Decimal("20.00"),
        "estimated_cost_inc": Decimal("50.00"),
        "estimated_cost_ex": Decimal("40.00"),
        "actual_lines": 4,
        "estimated_lines": 2,
        "missing_cost_lines": 8,
        "known_cost_total": Decimal("240.00"),
        "cost_quality": "incomplete",
    }


@pytest.mark.parametrize(
    ("records", "expected"),
    [
        ([("direct", "inc", Decimal("1"))], "actual_only"),
        ([("sales_ref", "inc", Decimal("1"))], "contains_estimate"),
        (
            [
                ("direct", "inc", Decimal("1")),
                ("trace_avg", "ex", Decimal("1")),
                ("none", None, None),
            ],
            "incomplete",
        ),
    ],
)
def test_cost_quality_states(records, expected):
    assert maintenance_cost_quality.summarize_records(records)["cost_quality"] == expected


def test_aggregate_summary_keeps_cent_quantization_after_fast_return():
    summary = maintenance_cost_quality.summarize_aggregate(
        lines=2,
        actual_cost_inc=Decimal("1.006"),
        actual_cost_ex=Decimal("2"),
        estimated_cost_inc=Decimal("0"),
        estimated_cost_ex=Decimal("3.004"),
        actual_lines=1,
        estimated_lines=1,
        missing_cost_lines=0,
    )

    assert summary["actual_cost_inc"] == Decimal("1.01")
    assert summary["actual_cost_ex"] == Decimal("2.00")
    assert summary["estimated_cost_inc"] == Decimal("0.00")
    assert summary["estimated_cost_ex"] == Decimal("3.00")
    assert summary["known_cost_total"] == Decimal("6.01")


def test_empty_aggregate_is_incomplete_instead_of_fabricating_complete_zero_cost():
    summary = maintenance_cost_quality.summarize_aggregate(
        lines=0,
        actual_cost_inc=Decimal("0"),
        actual_cost_ex=Decimal("0"),
        estimated_cost_inc=Decimal("0"),
        estimated_cost_ex=Decimal("0"),
        actual_lines=0,
        estimated_lines=0,
        missing_cost_lines=0,
    )

    assert summary["known_cost_total"] == Decimal("0.00")
    assert summary["cost_quality"] == "incomplete"


def test_tax_estimate_flags_cannot_promote_an_unknown_cost_source(db):
    """脏 flag 不能把 bucket=missing 的双税金额伪装成完整估算成本。"""
    batch = SysImportBatch(
        filename="unknown-dual-cost.xlsx",
        file_type="maintenance",
        file_hash="unknown-dual-cost",
        status="success",
    )
    db.add(batch)
    db.flush()
    loader.load(
        db,
        f.maintenance_result(
            {
                "M1": f.maintenance_head(
                    "M1",
                    on=date(2026, 3, 10),
                    project="未知成本来源项目",
                ),
            },
            [f.maintenance_line("M1", "ML-UNKNOWN-DUAL", "PN-UNKNOWN-DUAL", qty="1")],
        ),
        batch.id,
        date(2026, 6, 1),
    )
    line = db.scalar(
        select(FMaintenanceLine).where(
            FMaintenanceLine.raw_line_id == "ML-UNKNOWN-DUAL",
        )
    )
    line.cost_source = "future_source"
    line.cost_tax_basis = "ex"
    line.unit_cost = Decimal("100")
    line.cost_amount = Decimal("100")
    line.unit_cost_inc_tax = Decimal("113")
    line.unit_cost_ex_tax = Decimal("100")
    line.cost_amount_inc_tax = Decimal("113")
    line.cost_amount_ex_tax = Decimal("100")
    line.anomaly_flags = [
        "tax_rate_estimated",
        "inc_tax_estimated",
        "ex_tax_estimated",
    ]
    db.commit()

    row = maintenance_cost.projects_aggregate(db, lifecycle="all")["rows"][0]

    assert row["parts_cost_inc_tax"] == 0.0
    assert row["parts_cost_ex_tax"] == 0.0
    assert row["parts_cost_inc_tax_quality"] == "incomplete"
    assert row["parts_cost_ex_tax_quality"] == "incomplete"
    assert row["parts_cost_inc_tax_missing_lines"] == 1
    assert row["parts_cost_ex_tax_missing_lines"] == 1


def test_incomplete_cost_blocks_budget_decision_and_remaining_values():
    summary = maintenance_cost_quality.summarize_records([
        ("direct", "inc", Decimal("800.00")),
        ("none", None, None),
    ])

    decision = maintenance_cost_quality.budget_decision(
        summary,
        budget=Decimal("1000.00"),
        expense_total=Decimal("50.00"),
        warn_pct=Decimal("0.20"),
    )

    assert decision == {
        "decision_status": "incomplete_cost",
        "known_spend_total": Decimal("850.00"),
        "remaining": None,
        "remaining_pct": None,
    }


def test_missing_expense_watermark_blocks_budget_remaining_without_fabricating_zero():
    summary = maintenance_cost_quality.summarize_records([
        ("direct", "inc", Decimal("800.00")),
    ])

    decision = maintenance_cost_quality.budget_decision(
        summary,
        budget=Decimal("1000.00"),
        expense_total=Decimal("0.00"),
        expense_data_available=False,
        warn_pct=Decimal("0.20"),
    )

    assert decision == {
        "decision_status": "expense_data_unavailable",
        "known_spend_total": Decimal("800.00"),
        "remaining": None,
        "remaining_pct": None,
    }


@pytest.mark.parametrize(
    ("known_cost", "budget", "expected_status", "expected_remaining"),
    [
        ("1000.00", "1000.00", "red", "0.00"),
        ("800.00", "1000.00", "yellow", "200.00"),
        ("799.99", "1000.00", "green", "200.01"),
        ("100.00", None, "no_budget", None),
        ("100.00", "0.00", "no_budget", None),
        ("100.00", "-1.00", "no_budget", None),
    ],
)
def test_complete_cost_preserves_twenty_percent_budget_boundaries(
    known_cost,
    budget,
    expected_status,
    expected_remaining,
):
    summary = maintenance_cost_quality.summarize_records([
        ("direct", "inc", Decimal(known_cost)),
    ])

    decision = maintenance_cost_quality.budget_decision(
        summary,
        budget=Decimal(budget) if budget is not None else None,
        warn_pct=Decimal("0.20"),
    )

    assert decision["decision_status"] == expected_status
    assert decision["remaining"] == (
        Decimal(expected_remaining) if expected_remaining is not None else None
    )


def test_projects_aggregate_exposes_one_source_of_cost_quality_truth(db):
    batch = SysImportBatch(
        filename="quality.xlsx",
        file_type="maintenance",
        file_hash="issue156-quality",
        status="success",
    )
    db.add(batch)
    db.flush()
    loader.load(
        db,
        f.maintenance_result(
            {
                "M1": f.maintenance_head(
                    "M1",
                    on=date(2026, 3, 10),
                    project="成本分层项目",
                ),
            },
            [
                f.maintenance_line("M1", "ML-A", "PN-A", qty="1"),
                f.maintenance_line("M1", "ML-E", "PN-E", qty="1"),
                f.maintenance_line("M1", "ML-M", "PN-M", qty="1"),
                f.maintenance_line("M1", "ML-NULL", "PN-NULL", qty="1"),
                f.maintenance_line("M1", "ML-UNKNOWN", "PN-UNKNOWN", qty="1"),
                f.maintenance_line("M1", "ML-BASIS", "PN-BASIS", qty="1"),
                f.maintenance_line("M1", "ML-SOURCE-NULL", "PN-SOURCE-NULL", qty="1"),
                f.maintenance_line("M1", "ML-BASIS-NULL", "PN-BASIS-NULL", qty="1"),
                f.maintenance_line("M1", "ML-NEGATIVE", "PN-NEGATIVE", qty="1"),
                f.maintenance_line("M1", "ML-ZERO", "PN-ZERO", qty="1"),
            ],
        ),
        batch.id,
        date(2026, 6, 1),
    )
    lines = {
        line.raw_line_id: line
        for line in db.execute(select(FMaintenanceLine)).scalars()
    }
    lines["ML-A"].cost_source = "direct"
    lines["ML-A"].cost_tax_basis = "inc"
    lines["ML-A"].cost_amount = Decimal("100.00")
    lines["ML-E"].cost_source = "trace_avg"
    lines["ML-E"].cost_tax_basis = "ex"
    lines["ML-E"].cost_amount = Decimal("50.00")
    lines["ML-M"].cost_source = "none"
    lines["ML-NULL"].cost_source = "direct"
    lines["ML-NULL"].cost_tax_basis = "inc"
    lines["ML-UNKNOWN"].cost_source = "future_source"
    lines["ML-UNKNOWN"].cost_tax_basis = "inc"
    lines["ML-UNKNOWN"].unit_cost = Decimal("999.00")
    lines["ML-UNKNOWN"].cost_amount = Decimal("999.00")
    lines["ML-BASIS"].cost_source = "window"
    lines["ML-BASIS"].cost_tax_basis = "bad"
    lines["ML-BASIS"].unit_cost = Decimal("10.00")
    lines["ML-BASIS"].cost_amount = Decimal("10.00")
    lines["ML-SOURCE-NULL"].cost_source = None
    lines["ML-SOURCE-NULL"].cost_tax_basis = "inc"
    lines["ML-SOURCE-NULL"].unit_cost = Decimal("20.00")
    lines["ML-SOURCE-NULL"].cost_amount = Decimal("20.00")
    lines["ML-BASIS-NULL"].cost_source = "direct"
    lines["ML-BASIS-NULL"].cost_tax_basis = None
    lines["ML-BASIS-NULL"].unit_cost = Decimal("30.00")
    lines["ML-BASIS-NULL"].cost_amount = Decimal("30.00")
    lines["ML-NEGATIVE"].cost_source = "direct"
    lines["ML-NEGATIVE"].cost_tax_basis = "inc"
    lines["ML-NEGATIVE"].unit_cost = Decimal("-5.00")
    lines["ML-NEGATIVE"].cost_amount = Decimal("-5.00")
    lines["ML-ZERO"].cost_source = "direct"
    lines["ML-ZERO"].cost_tax_basis = "ex"
    lines["ML-ZERO"].unit_cost = Decimal("0.00")
    lines["ML-ZERO"].cost_amount = Decimal("0.00")
    db.commit()

    for line in lines.values():
        db.refresh(line)
        assert line.cost_bucket == maintenance_cost_quality.cost_bucket(
            line.cost_source,
            line.cost_tax_basis,
            line.cost_amount,
            line.confidence,
        )

    row = maintenance_cost.projects_aggregate(
        db,
        lifecycle="all",
    )["rows"][0]

    assert "cost_bucket" not in row
    assert row["actual_cost_inc"] == 100.0
    assert row["actual_cost_ex"] == 0.0
    assert row["estimated_cost_inc"] == 0.0
    assert row["estimated_cost_ex"] == 50.0
    assert row["actual_lines"] == 2
    assert row["estimated_lines"] == 1
    assert row["missing_cost_lines"] == 7
    assert row["known_cost_total"] == row["cost_total"] == 150.0
    assert row["cost_quality"] == "incomplete"
    assert (
        row["actual_lines"]
        + row["estimated_lines"]
        + row["missing_cost_lines"]
        == row["lines"]
    )
    assert row["by_source"] == {
        "direct": 2,
        "window": 0,
        "month_avg": 0,
        "trace_avg": 1,
        "sales_ref": 0,
        "pool_purchase": 0,
        "pool_sales": 0,
            "purchase_history": 0,
            "sales_history": 0,
            "manual": 0,
            "none": 7,
        }
    assert sum(row["by_source"].values()) == row["lines"]
    assert row["by_source"]["none"] == row["missing_cost_lines"]
    # 历史手工事实没有新双税列，normalized 口径必须 fail-closed 为不完整，不能拿
    # legacy 原始税口径静默冒充双税结果。
    assert row["parts_cost_inc_tax"] == 0.0
    assert row["parts_cost_ex_tax"] == 0.0
    assert row["parts_cost_inc_tax_complete"] is False
    assert row["parts_cost_ex_tax_complete"] is False
    assert row["parts_cost_inc_tax_quality"] == "incomplete"
    assert row["parts_cost_ex_tax_quality"] == "incomplete"

    actual, estimated, missing = maintenance_cost_quality.sql_tier_predicates(
        FMaintenanceLine.cost_source,
        FMaintenanceLine.cost_tax_basis,
        FMaintenanceLine.cost_amount,
    )
    predicates = (actual, estimated, missing)
    tier_counts = [
        db.scalar(select(func.count()).select_from(FMaintenanceLine).where(predicate))
        for predicate in predicates
    ]
    total_lines = db.scalar(select(func.count()).select_from(FMaintenanceLine))
    assert tier_counts == [2, 1, 7]
    assert sum(tier_counts) == total_lines
    for index, left in enumerate(predicates):
        for right in predicates[index + 1:]:
            assert db.scalar(
                select(func.count()).select_from(FMaintenanceLine).where(and_(left, right)),
            ) == 0

    detail_rows = {
        item["pn_std"]: item
        for item in maintenance_cost.project_lines(
            db,
            "成本分层项目",
            page_size=100,
        )["rows"]
    }
    assert detail_rows["PN-A"]["cost_tier"] == "actual"
    assert detail_rows["PN-E"]["cost_tier"] == "estimated"
    assert all("cost_bucket" not in item for item in detail_rows.values())
    for pn in (
        "PN-M",
        "PN-NULL",
        "PN-UNKNOWN",
        "PN-BASIS",
        "PN-SOURCE-NULL",
        "PN-BASIS-NULL",
        "PN-NEGATIVE",
    ):
        assert detail_rows[pn]["cost_tier"] == "missing"
        assert detail_rows[pn]["unit_cost"] is None
        assert detail_rows[pn]["cost_amount"] is None
    assert detail_rows["PN-ZERO"]["cost_tier"] == "actual"
    assert detail_rows["PN-ZERO"]["unit_cost"] == 0.0
    assert detail_rows["PN-ZERO"]["cost_amount"] == 0.0

    quality_permissions = permissions.effective("readonly", {
        "page_maintenance": True,
        "data_purchase_cost": True,
        "data_profit": True,
    })
    db.add(SysUser(
        username="quality",
        role="readonly",
        password_hash=hash_password("pw123456"),
        is_active=True,
        permissions=quality_permissions,
    ))
    db.commit()
    agent_rows = {
        item["pn_std"]: item
        for item in tools.dispatch(
            db,
            "get_maintenance_lines",
            {"project": "成本分层项目"},
            security.UserContext(
                user_id="quality",
                role="readonly",
                permissions=quality_permissions,
                is_authenticated=True,
                authn="sys_user",
                token_version=0,
            ),
        )["rows"]
    }
    assert agent_rows["PN-A"]["cost_tier"] == "actual"
    assert agent_rows["PN-UNKNOWN"]["cost_tier"] == "missing"
    assert agent_rows["PN-UNKNOWN"]["cost_amount"] is None


def test_shared_contract_missing_cost_blocks_the_whole_contract_decision(db):
    batch = SysImportBatch(
        filename="contract-quality.xlsx",
        file_type="maintenance",
        file_hash="issue156-contract-quality",
        status="success",
    )
    db.add(batch)
    db.flush()
    loader.load(
        db,
        f.sales_result(
            {
                "S1": f.sales_head(
                    "S1",
                    order_no="XS-QUALITY",
                    amount_ex_tax=Decimal("1000.00"),
                ),
                "S2": f.sales_head(
                    "S2",
                    order_no="XS-COMPLETE",
                    amount_ex_tax=Decimal("1000.00"),
                ),
            },
            [
                f.sales_line("S1", "SL1", "PN-SALE", qty="1", price="1000"),
                f.sales_line("S2", "SL2", "PN-SALE-2", qty="1", price="1000"),
            ],
        ),
        batch.id,
        date(2026, 6, 1),
    )
    loader.load(
        db,
        f.maintenance_result(
            {
                "M1": f.maintenance_head(
                    "M1",
                    on=date(2026, 3, 10),
                    project="共享合同项目甲",
                    sales_order="XS-QUALITY",
                ),
                "M2": f.maintenance_head(
                    "M2",
                    on=date(2026, 3, 11),
                    project="共享合同项目乙",
                    sales_order="XS-QUALITY",
                ),
                "M3": f.maintenance_head(
                    "M3",
                    on=date(2026, 3, 12),
                    project="完整成本项目",
                    sales_order="XS-COMPLETE",
                ),
            },
            [
                f.maintenance_line("M1", "ML-KNOWN", "PN-KNOWN", qty="1"),
                f.maintenance_line("M2", "ML-MISSING", "PN-MISSING", qty="1"),
                f.maintenance_line("M3", "ML-COMPLETE", "PN-COMPLETE", qty="1"),
            ],
        ),
        batch.id,
        date(2026, 6, 1),
    )
    lines = {
        line.raw_line_id: line
        for line in db.execute(select(FMaintenanceLine)).scalars()
    }
    lines["ML-KNOWN"].cost_source = "direct"
    lines["ML-KNOWN"].cost_tax_basis = "inc"
    lines["ML-KNOWN"].cost_amount = Decimal("100.00")
    lines["ML-MISSING"].cost_source = "future_source"
    lines["ML-MISSING"].cost_tax_basis = "inc"
    lines["ML-MISSING"].unit_cost = Decimal("999.00")
    lines["ML-MISSING"].cost_amount = Decimal("999.00")
    lines["ML-MISSING"].confidence = "low"
    lines["ML-COMPLETE"].cost_source = "direct"
    lines["ML-COMPLETE"].cost_tax_basis = "inc"
    lines["ML-COMPLETE"].cost_amount = Decimal("100.00")
    db.commit()

    all_rows = maintenance_cost.board(db, lifecycle="all")["rows"]
    assert [item["decision_status"] for item in all_rows] == [
        "incomplete_cost",
        "expense_data_unavailable",
    ]
    assert all("cost_bucket" not in item for item in all_rows)
    assert all(
        "cost_bucket" not in project
        for item in all_rows
        for project in item["projects"]
    )
    row = next(item for item in all_rows if item["contract"] == "XS-QUALITY")

    assert row["contract"] == "XS-QUALITY"
    assert row["cost_quality"] == "incomplete"
    assert row["actual_lines"] == 1
    assert row["estimated_lines"] == 0
    assert row["missing_cost_lines"] == 1
    assert (
        row["actual_lines"]
        + row["estimated_lines"]
        + row["missing_cost_lines"]
        == row["lines"]
    )
    assert all(
        project["actual_lines"]
        + project["estimated_lines"]
        + project["missing_cost_lines"]
        == project["lines"]
        for project in row["projects"]
    )
    assert row["known_cost_total"] == row["spent_parts"] == 100.0
    assert row["low_conf_pct"] == 0.0
    assert row["decision_status"] == row["status"] == "incomplete_cost"
    assert row["remaining"] is None
    assert row["remaining_pct"] is None

    engine = db.get_bind()
    select_count = 0

    def before_execute(_conn, _cursor, statement, _params, _context, _many):
        nonlocal select_count
        if statement.lstrip().upper().startswith("SELECT"):
            select_count += 1

    event.listen(engine, "before_cursor_execute", before_execute)
    try:
        searched_rows = maintenance_cost.board(
            db,
            q_text="项目甲",
            lifecycle="all",
        )["rows"]
    finally:
        event.remove(engine, "before_cursor_execute", before_execute)
    # 双口径贡献毛利新增合同费用快照水位查询；仍保持固定查询数，不随项目数增长。
    assert select_count <= 4
    assert [item["contract"] for item in searched_rows] == ["XS-QUALITY"]
    assert {project["project"] for project in searched_rows[0]["projects"]} == {
        "共享合同项目甲",
        "共享合同项目乙",
    }
    assert searched_rows[0]["decision_status"] == "incomplete_cost"
    assert searched_rows[0]["missing_cost_lines"] == 1

    filtered_rows = maintenance_cost.board(
        db,
        status="incomplete_cost",
        lifecycle="all",
    )["rows"]
    assert [item["contract"] for item in filtered_rows] == ["XS-QUALITY"]

    workbook_data = maintenance_cost.contract_workbook_data(db, "XS-QUALITY")
    assert workbook_data["cost_summary"]["cost_quality"] == "incomplete"
    assert workbook_data["cost_summary"]["known_cost_total"] == Decimal("100.00")
    assert workbook_data["decision"]["decision_status"] == "incomplete_cost"
    assert workbook_data["decision"]["remaining"] is None

    workbook = maintenance_workbook_renderer.render_contract_workbook(
        "XS-QUALITY",
        workbook_data,
        lambda value: value,
    )
    try:
        budget_sheet = workbook["项目预算"]
        rendered_text = "\n".join(
            str(cell.value)
            for row_cells in budget_sheet.iter_rows()
            for cell in row_cells
            if cell.value is not None
        )
        assert "成本不完整，需补数据" in rendered_text
        assert "实际采购参考（含税）" in rendered_text
        assert "估算参考（不含税）" in rendered_text
        assert "缺失成本行" in rendered_text
        assert "已知备件成本参考（混合原值·兼容）" in rendered_text
        assert not any(word in rendered_text for word in ("健康", "亏损", "超支"))
        remaining_label = next(
            cell
            for row_cells in budget_sheet.iter_rows()
            for cell in row_cells
            if cell.value == "剩余预算"
        )
        assert budget_sheet.cell(
            remaining_label.row,
            remaining_label.column + 1,
        ).value in (None, "—")
        assert workbook["备件明细-氚云"].cell(1, 13).value == "已知成本参考"
    finally:
        workbook.close()
