"""Sales-manager replenishment cart Beta business contracts."""

from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from io import BytesIO
from threading import Barrier

from fastapi.testclient import TestClient
from openpyxl import load_workbook
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from app import permissions
from app.auth import hash_password
from app.config import get_settings
from app.db import SessionLocal
from app.main import app
from app.models.data_quality import FactDataQualityIssue
from app.models.dimensions import DimPart
from app.models.inventory import Inventory
from app.models.purchase import FPurchaseLine, FPurchaseOrder
from app.models.replenishment import (
    ReplenishmentApplication,
    ReplenishmentApplicationLine,
    ReplenishmentApplicationVersion,
    ReplenishmentReview,
)
from app.models.sales import FSalesLine, FSalesOrder
from app.models.system import SysImportBatch, SysUser
from app.services import replenishment


def test_beta_permissions_are_additive_to_the_legacy_role_graph():
    page_key = "page_replenishment_beta"
    create_key = "action_replenishment_create"
    review_key = "action_replenishment_review"

    assert page_key in permissions.PAGE_KEYS
    assert create_key in permissions.ACTION_KEYS
    assert review_key in permissions.ACTION_KEYS
    assert permissions.effective("admin", None)[page_key] is True
    assert permissions.effective("admin", None)[create_key] is True
    assert permissions.effective("admin", None)[review_key] is True
    assert review_key not in permissions.ACTION_PAGE_DEPENDENCIES
    assert review_key not in permissions.ACTION_DATA_DEPENDENCIES
    for role in ("boss", "sales", "purchaser", "readonly", "guest"):
        assert permissions.effective(role, None)[page_key] is False
        assert permissions.effective(role, None)[create_key] is False
        assert permissions.effective(role, None)[review_key] is False


def _user(db, username: str = "sales_manager") -> SysUser:
    user = SysUser(
        username=username,
        password_hash="not-used-by-service-test",
        role="admin",
        display_name="销售经理测试",
        salesperson_name="销售经理测试",
        is_active=True,
    )
    db.add(user)
    db.flush()
    return user


def test_server_feature_gate_closes_business_api_without_hiding_beta_state(db):
    base = permissions.effective("admin", None)
    base["page_replenishment_beta"] = False
    user = SysUser(
        username="beta_gate_admin",
        password_hash=hash_password("safe-test-password"),
        role="admin",
        display_name="Beta Gate Admin",
        template_code="admin",
        template_version=1,
        template_perms=base,
        perm_overrides={"page_replenishment_beta": True},
        is_active=True,
    )
    db.add(user)
    db.commit()
    client = TestClient(app)
    login = client.post(
        "/api/auth/login",
        json={"username": user.username, "password": "safe-test-password"},
    )
    assert login.status_code == 200
    client.headers["Authorization"] = f"Bearer {login.json()['token']}"

    settings = get_settings()
    original = settings.replenishment_beta_enabled
    try:
        settings.replenishment_beta_enabled = False
        capabilities = client.get("/api/replenishment-beta/capabilities")
        assert capabilities.status_code == 200
        assert capabilities.json()["enabled"] is False
        assert capabilities.headers["cache-control"] == "no-store"
        assert client.get("/api/replenishment-beta/catalog").status_code == 404
        assert client.get("/api/replenishment-beta/applications").status_code == 404
    finally:
        settings.replenishment_beta_enabled = original


def test_shared_fallback_identity_is_rejected_by_every_beta_surface(db):
    """A signed shared admin token is still not a named accountable operator."""
    client = TestClient(app)
    login = client.post(
        "/api/auth/login",
        json={"username": "admin", "password": get_settings().admin_password},
    )
    assert login.status_code == 200
    client.headers["Authorization"] = f"Bearer {login.json()['token']}"

    settings = get_settings()
    original = settings.replenishment_beta_enabled
    try:
        settings.replenishment_beta_enabled = True
        calls = [
            ("get", "/api/replenishment-beta/capabilities", None),
            ("get", "/api/replenishment-beta/catalog", None),
            ("get", "/api/replenishment-beta/applications", None),
            ("post", "/api/replenishment-beta/applications", {}),
            (
                "get",
                "/api/replenishment-beta/applications/not-real/exports/manual-review.xlsx",
                None,
            ),
            (
                "get",
                "/api/replenishment-beta/applications/not-real/exports/wbdd-subset.xlsx",
                None,
            ),
            (
                "post",
                "/api/replenishment-beta/applications/not-real/review-results",
                {
                    "version_id": "00000000-0000-0000-0000-000000000000",
                    "content_digest": "0" * 64,
                    "idempotency_key": "shared-identity-block",
                    "decisions": [
                        {
                            "line_id": "00000000-0000-0000-0000-000000000000",
                            "decision": "approved",
                        }
                    ],
                },
            ),
        ]
        for method, path, body in calls:
            response = getattr(client, method)(path, json=body) if body is not None else getattr(client, method)(path)
            assert response.status_code == 403, (method, path, response.text)
            assert "实名系统账号" in response.text
    finally:
        settings.replenishment_beta_enabled = original


def test_application_owner_scope_is_row_isolated(db):
    owner = _user(db, "replenishment_owner")
    other = SysUser(
        username="other_sales_manager",
        password_hash="not-used-by-service-test",
        role="sales",
        display_name="其他销售经理",
        is_active=True,
    )
    db.add(other)
    db.commit()
    created = replenishment.create_application(
        db,
        username=owner.username,
        warehouse="北京前置库",
        request_note=None,
    )

    assert replenishment.list_applications(
        db, username=other.username, role=other.role, page=1, page_size=20
    )["total"] == 0
    with pytest.raises(replenishment.ReplenishmentError) as exc_info:
        replenishment.get_application(
            db,
            created["application_id"],
            username=other.username,
            role=other.role,
        )
    assert exc_info.value.status_code == 404


def test_price_facts_require_data_permission_even_when_page_is_granted(db):
    password = "safe-test-password"
    custom = permissions.effective("readonly", None)
    custom.update(
        {
            "page_replenishment_beta": True,
            "data_pool_price_governance": False,
            # Simulate a legacy dirty permission graph: runtime endpoints must
            # still enforce the data dependency instead of trusting the action.
            "action_replenishment_create": True,
        }
    )
    user = SysUser(
        username="beta_price_blind",
        password_hash=hash_password(password),
        role="readonly",
        display_name="价格不可见试用账号",
        permissions=custom,
        is_active=True,
    )
    db.add(user)
    db.commit()
    client = TestClient(app)
    login = client.post("/api/auth/login", json={"username": user.username, "password": password})
    assert login.status_code == 200
    client.headers["Authorization"] = f"Bearer {login.json()['token']}"

    settings = get_settings()
    original = settings.replenishment_beta_enabled
    try:
        settings.replenishment_beta_enabled = True
        capability = client.get("/api/replenishment-beta/capabilities")
        assert capability.status_code == 200
        assert capability.json()["can_view_price"] is False
        assert capability.json()["can_create"] is False
        calls = [
            ("get", "/api/replenishment-beta/catalog", None),
            ("get", "/api/replenishment-beta/applications", None),
            ("get", "/api/replenishment-beta/applications/not-real", None),
            ("post", "/api/replenishment-beta/applications", {}),
            (
                "get",
                "/api/replenishment-beta/applications/not-real/exports/manual-review.xlsx",
                None,
            ),
            (
                "get",
                "/api/replenishment-beta/applications/not-real/exports/wbdd-subset.xlsx",
                None,
            ),
        ]
        for method, path, body in calls:
            response = getattr(client, method)(path, json=body) if body is not None else getattr(client, method)(path)
            assert response.status_code == 403, (method, path, response.text)
    finally:
        settings.replenishment_beta_enabled = original


def test_review_callback_requires_page_allowlist_and_action(db):
    owner = _user(db, "review_callback_owner")
    part = DimPart(pn_std="REVIEW-CALLBACK-PN", status="active")
    reviewer_password = "safe-review-password"
    reviewer_perms = permissions.effective("readonly", None)
    reviewer_perms.update(
        {
            "page_replenishment_beta": False,
            "data_pool_price_governance": False,
            "action_replenishment_review": True,
        }
    )
    reviewer = SysUser(
        username="review_callback_account",
        password_hash=hash_password(reviewer_password),
        role="readonly",
        display_name="受控审核回调账号",
        permissions=reviewer_perms,
        is_active=True,
    )
    db.add_all([part, reviewer])
    db.commit()
    created = replenishment.create_application(
        db, username=owner.username, warehouse="北京前置库", request_note=None
    )
    created = replenishment.add_line(
        db,
        created["application_id"],
        username=owner.username,
        role=owner.role,
        expected_version=created["version"],
        part_id=part.id,
        quantity=1,
    )
    submitted = replenishment.submit(
        db,
        created["application_id"],
        username=owner.username,
        role=owner.role,
        expected_version=created["version"],
    )
    version = submitted["versions"][0]

    client = TestClient(app)
    login = client.post(
        "/api/auth/login",
        json={"username": reviewer.username, "password": reviewer_password},
    )
    assert login.status_code == 200
    client.headers["Authorization"] = f"Bearer {login.json()['token']}"
    settings = get_settings()
    original = settings.replenishment_beta_enabled
    try:
        settings.replenishment_beta_enabled = True
        assert client.get("/api/replenishment-beta/capabilities").status_code == 403
        response = client.post(
            f"/api/replenishment-beta/applications/{created['application_id']}/review-results",
            json={
                "version_id": version["version_id"],
                "content_digest": version["content_digest"],
                "idempotency_key": "controlled-review-callback",
                "decisions": [
                    {
                        "line_id": version["lines"][0]["line_id"],
                        "decision": "approved",
                    }
                ],
            },
        )
        assert response.status_code == 403, response.text
        assert "未获得补库申请页面权限" in response.text

        # Explicitly adding the Beta page bit admits this named reviewer to the
        # callback without granting access to governed price facts.
        reviewer.permissions = {
            **reviewer_perms,
            "page_replenishment_beta": True,
        }
        reviewer.token_version = (reviewer.token_version or 0) + 1
        db.commit()
        login = client.post(
            "/api/auth/login",
            json={"username": reviewer.username, "password": reviewer_password},
        )
        assert login.status_code == 200
        client.headers["Authorization"] = f"Bearer {login.json()['token']}"
        assert client.get("/api/replenishment-beta/capabilities").status_code == 200
        assert client.get("/api/replenishment-beta/catalog").status_code == 403
        response = client.post(
            f"/api/replenishment-beta/applications/{created['application_id']}/review-results",
            json={
                "version_id": version["version_id"],
                "content_digest": version["content_digest"],
                "idempotency_key": "controlled-review-callback",
                "decisions": [
                    {
                        "line_id": version["lines"][0]["line_id"],
                        "decision": "approved",
                    }
                ],
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["application_status"] == "approved"
    finally:
        settings.replenishment_beta_enabled = original


def test_review_callback_blocks_submitter_even_when_named_admin_has_all_permissions(db):
    password = "safe-sod-admin-password"
    reviewer_password = "safe-sod-reviewer-password"
    submitter = SysUser(
        username="replenishment_sod_admin",
        password_hash=hash_password(password),
        role="admin",
        display_name="补库提交管理员",
        permissions=permissions.effective("admin", None),
        is_active=True,
    )
    reviewer_permissions = permissions.effective("readonly", None)
    reviewer_permissions.update(
        {
            "page_replenishment_beta": True,
            "action_replenishment_review": True,
        }
    )
    reviewer = SysUser(
        username="replenishment_sod_reviewer",
        password_hash=hash_password(reviewer_password),
        role="readonly",
        display_name="补库独立审核人",
        permissions=reviewer_permissions,
        is_active=True,
    )
    part = DimPart(pn_std="REPLENISHMENT-SOD-PN", status="active")
    db.add_all([submitter, reviewer, part])
    db.commit()
    created = replenishment.create_application(
        db,
        username=submitter.username,
        warehouse="北京前置库",
        request_note=None,
    )
    created = replenishment.add_line(
        db,
        created["application_id"],
        username=submitter.username,
        role=submitter.role,
        expected_version=created["version"],
        part_id=part.id,
        quantity=1,
    )
    submitted = replenishment.submit(
        db,
        created["application_id"],
        username=submitter.username,
        role=submitter.role,
        expected_version=created["version"],
    )
    version = submitted["versions"][0]
    assert version["submitted_by"] == submitter.username

    client = TestClient(app)
    login = client.post(
        "/api/auth/login",
        json={"username": submitter.username, "password": password},
    )
    assert login.status_code == 200
    client.headers["Authorization"] = f"Bearer {login.json()['token']}"

    settings = get_settings()
    original = settings.replenishment_beta_enabled
    try:
        settings.replenishment_beta_enabled = True
        response = client.post(
            f"/api/replenishment-beta/applications/{created['application_id']}/review-results",
            json={
                "version_id": version["version_id"],
                "content_digest": version["content_digest"],
                "idempotency_key": "same-admin-self-review",
                "decisions": [
                    {
                        "line_id": version["lines"][0]["line_id"],
                        "decision": "approved",
                    }
                ],
            },
        )
        assert response.status_code == 409, response.text
        assert response.json()["detail"]["code"] == "separation_of_duties"
        assert "提交人与审核人不能是同一账号" in response.text
    finally:
        settings.replenishment_beta_enabled = original

    db.expire_all()
    persisted_application = db.get(
        ReplenishmentApplication,
        created["application_id"],
    )
    persisted_version = db.get(
        ReplenishmentApplicationVersion,
        version["version_id"],
    )
    assert persisted_application.status == "submitted"
    assert persisted_version.status == "submitted"
    assert persisted_version.submitted_by == submitter.username
    assert db.scalar(select(func.count()).select_from(ReplenishmentReview)) == 0

    login = client.post(
        "/api/auth/login",
        json={"username": reviewer.username, "password": reviewer_password},
    )
    assert login.status_code == 200
    client.headers["Authorization"] = f"Bearer {login.json()['token']}"
    settings.replenishment_beta_enabled = True
    try:
        distinct_review = client.post(
            f"/api/replenishment-beta/applications/{created['application_id']}/review-results",
            json={
                "version_id": version["version_id"],
                "content_digest": version["content_digest"],
                "idempotency_key": "distinct-reviewer-result",
                "decisions": [
                    {
                        "line_id": version["lines"][0]["line_id"],
                        "decision": "approved",
                    }
                ],
            },
        )
        assert distinct_review.status_code == 200, distinct_review.text
        assert distinct_review.json()["application_status"] == "approved"

        login = client.post(
            "/api/auth/login",
            json={"username": submitter.username, "password": password},
        )
        assert login.status_code == 200
        client.headers["Authorization"] = f"Bearer {login.json()['token']}"
        self_replay = client.post(
            f"/api/replenishment-beta/applications/{created['application_id']}/review-results",
            json={
                "version_id": version["version_id"],
                "content_digest": version["content_digest"],
                "idempotency_key": "distinct-reviewer-result",
                "decisions": [
                    {
                        "line_id": version["lines"][0]["line_id"],
                        "decision": "approved",
                    }
                ],
            },
        )
        assert self_replay.status_code == 409, self_replay.text
        assert self_replay.json()["detail"]["code"] == "separation_of_duties"
    finally:
        settings.replenishment_beta_enabled = original

    db.expire_all()
    review = db.scalar(
        select(ReplenishmentReview).where(
            ReplenishmentReview.version_id == version["version_id"]
        )
    )
    assert review.reviewed_by == reviewer.username


def test_free_text_bounds_do_not_reflect_business_input(db):
    password = "safe-test-password"
    base = permissions.effective("admin", None)
    base["page_replenishment_beta"] = False
    user = SysUser(
        username="beta_text_admin",
        password_hash=hash_password(password),
        role="admin",
        display_name="Beta Text Admin",
        template_code="admin",
        template_version=1,
        template_perms=base,
        perm_overrides={"page_replenishment_beta": True},
        is_active=True,
    )
    db.add(user)
    db.commit()
    client = TestClient(app)
    login = client.post("/api/auth/login", json={"username": user.username, "password": password})
    assert login.status_code == 200
    client.headers["Authorization"] = f"Bearer {login.json()['token']}"
    secret_note = "SENSITIVE-REPLENISHMENT-NOTE-" + "x" * 4000
    secret_query = "SENSITIVE-CATALOG-QUERY-" + "x" * 129
    settings = get_settings()
    original = settings.replenishment_beta_enabled
    try:
        settings.replenishment_beta_enabled = True
        note_response = client.post(
            "/api/replenishment-beta/applications", json={"request_note": secret_note}
        )
        assert note_response.status_code == 400
        assert "SENSITIVE-REPLENISHMENT-NOTE" not in note_response.text
        query_response = client.get("/api/replenishment-beta/catalog", params={"q": secret_query})
        assert query_response.status_code == 422
        assert "SENSITIVE-CATALOG-QUERY" not in query_response.text
    finally:
        settings.replenishment_beta_enabled = original


def test_catalog_keeps_no_pool_no_price_part_visible(db):
    db.add(DimPart(pn_std="NO-POOL-001", description="无池但仍可选", status="active"))
    db.commit()

    result = replenishment.catalog_search(db, "NO-POOL")

    assert result["total"] == 1
    item = result["items"][0]
    assert item["pn_std"] == "NO-POOL-001"
    assert item["pool"] == {"group_id": None, "name": None, "version": None}
    assert item["purchase"] is None
    assert item["sales"] is None
    assert item["price_window"]["days"] == 180


def test_catalog_price_facts_do_not_depend_on_pool_and_exclude_confirmed_source_error(db):
    part = DimPart(pn_std="PRICE-WITHOUT-POOL", status="active")
    purchase_batch = SysImportBatch(filename="p.xlsx", file_type="purchase", file_hash="replenishment-p")
    sales_batch = SysImportBatch(filename="s.xlsx", file_type="sales", file_hash="replenishment-s")
    db.add_all([part, purchase_batch, sales_batch])
    db.flush()
    purchase_ok = FPurchaseOrder(
        raw_order_id="rp-ok",
        order_no="RP-OK",
        order_date=date(2026, 7, 1),
        is_tax_inclusive=False,
        data_status="已生效",
        import_batch_id=purchase_batch.id,
    )
    purchase_bad = FPurchaseOrder(
        raw_order_id="rp-bad",
        order_no="RP-BAD",
        order_date=date(2026, 7, 2),
        is_tax_inclusive=False,
        data_status="已生效",
        import_batch_id=purchase_batch.id,
    )
    sales_order = FSalesOrder(
        raw_order_id="rs-ok",
        order_no="RS-OK",
        order_date=date(2026, 7, 3),
        data_status="已生效",
        import_batch_id=sales_batch.id,
    )
    db.add_all([purchase_ok, purchase_bad, sales_order])
    db.flush()
    line_ok = FPurchaseLine(
        raw_line_id="rpl-ok",
        order_id=purchase_ok.id,
        part_id=part.id,
        pn_std=part.pn_std,
        qty=2,
        unit_price=100,
        import_batch_id=purchase_batch.id,
    )
    line_bad = FPurchaseLine(
        raw_line_id="rpl-bad",
        order_id=purchase_bad.id,
        part_id=part.id,
        pn_std=part.pn_std,
        qty=1,
        unit_price=999,
        import_batch_id=purchase_batch.id,
    )
    sales_line = FSalesLine(
        raw_line_id="rsl-ok",
        order_id=sales_order.id,
        part_id=part.id,
        pn_std=part.pn_std,
        qty=3,
        unit_price=113,
        revenue_amount=300,
        counts_revenue=True,
        import_batch_id=sales_batch.id,
    )
    db.add_all([line_ok, line_bad, sales_line])
    db.flush()
    db.add(
        FactDataQualityIssue(
            side="purchase",
            line_id=line_bad.id,
            part_id=part.id,
            import_batch_id=purchase_batch.id,
            rule_code="synthetic_source_error",
            rule_version="test-v1",
            evidence={"synthetic": True},
            source_fingerprint="replenishment-price-source-error",
            status="confirmed_source_error",
            detected_by="test",
        )
    )
    db.commit()

    item = replenishment.catalog_search(
        db, "PRICE-WITHOUT-POOL", as_of=date(2026, 8, 10)
    )["items"][0]

    assert item["pool"]["group_id"] is None
    assert item["purchase"] == {
        "weighted_avg": 100.0,
        "total_qty": 2.0,
        "order_count": 1,
        "line_count": 1,
        "latest_date": "2026-07-01",
    }
    assert item["sales"] == {
        "weighted_avg": 100.0,
        "total_qty": 3.0,
        "order_count": 1,
        "line_count": 1,
        "latest_date": "2026-07-03",
    }


def test_removing_first_draft_line_compacts_numbers_without_unique_collision(db):
    user = _user(db, "remove_middle_owner")
    parts = [DimPart(pn_std=f"REMOVE-{index}", status="active") for index in range(1, 4)]
    db.add_all(parts)
    db.commit()
    application = replenishment.create_application(
        db, username=user.username, warehouse="北京前置库", request_note=None
    )
    for part in parts:
        application = replenishment.add_line(
            db,
            application["application_id"],
            username=user.username,
            role=user.role,
            expected_version=application["version"],
            part_id=part.id,
            quantity=1,
        )

    application = replenishment.remove_line(
        db,
        application["application_id"],
        application["versions"][0]["lines"][0]["line_id"],
        username=user.username,
        role=user.role,
        expected_version=application["version"],
    )

    assert [line["line_no"] for line in application["versions"][0]["lines"]] == [1, 2]
    assert [line["pn_std"] for line in application["versions"][0]["lines"]] == [
        "REMOVE-2",
        "REMOVE-3",
    ]


def test_concurrent_review_retry_is_idempotent(db):
    user = _user(db, "concurrent_review_user")
    reviewer = _user(db, "concurrent_review_reviewer")
    part = DimPart(pn_std="CONCURRENT-REVIEW-PN", status="active")
    db.add(part)
    db.commit()
    application = replenishment.create_application(
        db, username=user.username, warehouse="北京前置库", request_note=None
    )
    application = replenishment.add_line(
        db,
        application["application_id"],
        username=user.username,
        role=user.role,
        expected_version=application["version"],
        part_id=part.id,
        quantity=1,
    )
    submitted = replenishment.submit(
        db,
        application["application_id"],
        username=user.username,
        role=user.role,
        expected_version=application["version"],
    )
    version = submitted["versions"][0]
    barrier = Barrier(2)

    def callback() -> dict:
        with SessionLocal() as session:
            barrier.wait(timeout=10)
            return replenishment.record_review(
                session,
                submitted["application_id"],
                reviewer=reviewer.username,
                version_id=version["version_id"],
                content_digest=version["content_digest"],
                idempotency_key="same-concurrent-review-key",
                external_reference="agent-concurrency-test",
                summary_note=None,
                decisions=[
                    {
                        "line_id": version["lines"][0]["line_id"],
                        "decision": "approved",
                        "reason": None,
                    }
                ],
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = [future.result(timeout=20) for future in [executor.submit(callback), executor.submit(callback)]]

    assert {result["idempotent"] for result in results} == {False, True}
    assert len({result["review_id"] for result in results}) == 1


def test_replenishment_version_review_revision_and_exports_are_closed_loop(db):
    user = _user(db)
    reviewer = _user(db, "replenishment_reviewer")
    part_a = DimPart(pn_std="PN-A", description="\ufffe   =formula-like-description", unit="件", status="active")
    part_b = DimPart(pn_std="PN-B", description="正常描述", unit="件", status="active")
    db.add_all([part_a, part_b])
    db.commit()
    inventory_before = int(db.scalar(select(func.count()).select_from(Inventory)) or 0)

    application = replenishment.create_application(
        db,
        username=user.username,
        warehouse="北京前置库",
        request_note="首轮申请",
    )
    application = replenishment.add_line(
        db,
        application["application_id"],
        username=user.username,
        role="admin",
        expected_version=application["version"],
        part_id=part_a.id,
        quantity="2",
    )
    application = replenishment.add_line(
        db,
        application["application_id"],
        username=user.username,
        role="admin",
        expected_version=application["version"],
        part_id=part_b.id,
        quantity="3",
    )
    draft_model = db.get(
        ReplenishmentApplicationVersion,
        application["versions"][0]["version_id"],
    )
    draft_lines = list(
        db.scalars(
            select(ReplenishmentApplicationLine)
            .where(ReplenishmentApplicationLine.version_id == draft_model.version_id)
            .order_by(ReplenishmentApplicationLine.line_no)
        )
    )
    canonical = replenishment._submission_content(draft_model, draft_lines)
    assert set(canonical["lines"][0]) == {
        "line_id",
        "request_line_id",
        "version_id",
        "source_line_id",
        "line_no",
        "part_id",
        "pn_std",
        "description",
        "brand",
        "unit",
        "quantity",
        "special_note",
        "pool_group_id",
        "pool_name",
        "pool_version",
        "price_window_from",
        "price_window_to",
        "price_as_of",
        "purchase",
        "sales",
        "evidence_digest",
    }
    base_digest = replenishment._digest(canonical)
    frozen_field_changes = {
        "description": "另一份产品描述",
        "brand": "另一品牌",
        "unit": "套",
        "price_as_of": draft_lines[0].price_as_of + timedelta(days=1),
    }
    for field, changed in frozen_field_changes.items():
        original = getattr(draft_lines[0], field)
        setattr(draft_lines[0], field, changed)
        assert replenishment._digest(
            replenishment._submission_content(draft_model, draft_lines)
        ) != base_digest
        setattr(draft_lines[0], field, original)
    submitted = replenishment.submit(
        db,
        application["application_id"],
        username=user.username,
        role="admin",
        expected_version=application["version"],
    )
    version_one = submitted["versions"][0]
    assert submitted["status"] == "submitted"
    assert len(version_one["content_digest"]) == 64
    # Submitted lines are immutable at the database boundary, not only in the UI/service.
    with pytest.raises(DBAPIError, match="submitted replenishment lines are immutable"):
        db.execute(
            text("UPDATE replenishment_application_line SET quantity = 99 WHERE line_id = :line_id"),
            {"line_id": version_one["lines"][0]["line_id"]},
        )
        db.commit()
    db.rollback()

    decisions = [
        {"line_id": version_one["lines"][0]["line_id"], "decision": "rejected", "reason": "请说明特殊原因"},
        {"line_id": version_one["lines"][1]["line_id"], "decision": "approved", "reason": None},
    ]
    review = replenishment.record_review(
        db,
        submitted["application_id"],
        reviewer=reviewer.username,
        version_id=version_one["version_id"],
        content_digest=version_one["content_digest"],
        idempotency_key="review-v1-fixed",
        external_reference="agent-run-1",
        summary_note="一条打回",
        decisions=decisions,
    )
    assert review["rejected_count"] == 1
    replay = replenishment.record_review(
        db,
        submitted["application_id"],
        reviewer=reviewer.username,
        version_id=version_one["version_id"],
        content_digest=version_one["content_digest"],
        idempotency_key="review-v1-fixed",
        external_reference="agent-run-1",
        summary_note="一条打回",
        decisions=decisions,
    )
    assert replay["idempotent"] is True
    assert replay["application_status"] == review["application_status"] == "needs_revision"

    # Review-line provenance is also enforced at the database boundary: an
    # append-only row may not point at a line from another application/version.
    foreign = replenishment.create_application(
        db, username=user.username, warehouse="北京前置库", request_note=None
    )
    foreign = replenishment.add_line(
        db,
        foreign["application_id"],
        username=user.username,
        role="admin",
        expected_version=foreign["version"],
        part_id=part_a.id,
        quantity=1,
    )
    foreign = replenishment.submit(
        db,
        foreign["application_id"],
        username=user.username,
        role="admin",
        expected_version=foreign["version"],
    )
    with pytest.raises(DBAPIError, match="replenishment review line version mismatch"):
        db.execute(
            text(
                """
                INSERT INTO replenishment_review_line
                  (review_line_id, review_id, version_line_id, decision)
                VALUES
                  ('00000000-0000-0000-0000-000000000091', :review_id, :line_id, 'approved')
                """
            ),
            {
                "review_id": review["review_id"],
                "line_id": foreign["versions"][0]["lines"][0]["line_id"],
            },
        )
        db.commit()
    db.rollback()

    after_review = replenishment.get_application(
        db, submitted["application_id"], username=user.username, role="admin"
    )
    revision = replenishment.start_revision(
        db,
        submitted["application_id"],
        username=user.username,
        role="admin",
        expected_version=after_review["version"],
    )
    draft_two = revision["versions"][0]
    assert draft_two["version_no"] == 2
    assert len(draft_two["lines"]) == 1
    assert draft_two["lines"][0]["request_line_id"] == version_one["lines"][0]["request_line_id"]
    with pytest.raises(replenishment.ReplenishmentError) as exc_info:
        replenishment.remove_line(
            db,
            revision["application_id"],
            draft_two["lines"][0]["line_id"],
            username=user.username,
            role="admin",
            expected_version=revision["version"],
        )
    assert exc_info.value.code == "revision_line_required"

    revision = replenishment.update_line(
        db,
        revision["application_id"],
        draft_two["lines"][0]["line_id"],
        username=user.username,
        role="admin",
        expected_version=revision["version"],
        part_id=part_a.id,
        quantity="2",
        special_note="客户指定，需要按原 PN 补库",
    )
    submitted_two = replenishment.submit(
        db,
        revision["application_id"],
        username=user.username,
        role="admin",
        expected_version=revision["version"],
    )
    version_two = submitted_two["versions"][0]
    approved = replenishment.record_review(
        db,
        submitted_two["application_id"],
        reviewer=reviewer.username,
        version_id=version_two["version_id"],
        content_digest=version_two["content_digest"],
        idempotency_key="review-v2-fixed",
        external_reference="agent-run-2",
        summary_note="复提通过",
        decisions=[
            {"line_id": version_two["lines"][0]["line_id"], "decision": "approved", "reason": None}
        ],
    )
    assert approved["application_status"] == "approved"

    wbdd_bytes, filename = replenishment.wbdd_subset_workbook(
        db, submitted_two["application_id"], username=user.username, role="admin"
    )
    assert filename.endswith("-wbdd-subset.xlsx")
    workbook = load_workbook(BytesIO(wbdd_bytes), read_only=True)
    sheet = workbook["WBDD字段子集"]
    assert "录入辅助，非直接导入" in sheet["A1"].value
    assert [cell.value for cell in sheet[2]] == [
        "需求类型",
        "销售人员",
        "出库仓库(必填)",
        "需求明细.序号",
        "需求明细.需供货产品",
        "需求明细.产品描述",
        "需求明细.需求数量",
    ]
    assert sheet.max_row == 4  # notice + headers + two cumulatively approved intentions
    assert sheet["B3"].value == "销售经理测试"
    assert sheet["B4"].value == "销售经理测试"
    assert sheet["A3"].value == "补库供货"
    assert sheet["A4"].value == "补库供货"
    # Dynamic text that begins with '=' is neutralised before Excel receives it.
    assert sheet["F4"].value == "'   =formula-like-description"
    workbook.close()

    assert int(db.scalar(select(func.count()).select_from(Inventory)) or 0) == inventory_before
    submitted_line_ids = [item["line_id"] for item in version_one["lines"]]
    assert len(
        list(
            db.scalars(
                select(ReplenishmentApplicationLine).where(
                    ReplenishmentApplicationLine.line_id.in_(submitted_line_ids)
                )
            )
        )
    ) == 2
