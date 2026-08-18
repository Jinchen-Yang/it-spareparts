"""Sales-manager replenishment cart Beta business contracts."""

from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from io import BytesIO
from threading import Barrier
from uuid import uuid4

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
from app.models.maintenance_project import MaintenanceProject
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


def _project(db, code: str | None = None) -> MaintenanceProject:
    if code is None:
        code = f"RTEST-{uuid4().hex[:8].upper()}"
    project = MaintenanceProject(
        project_id=str(uuid4()),
        project_code=code,
        display_name=f"补库测试项目-{code}",
        lifecycle_status="ongoing",
        is_active=True,
        version=1,
    )
    db.add(project)
    db.flush()
    return project


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
        project_id=_project(db).project_id,
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
        db,
        username=owner.username,
        warehouse="北京前置库",
        request_note=None,
        project_id=_project(db).project_id,
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
        # 2026-08-18：旧 review-results 已停用（_retired → 410）；无页面权限时
        # 权限检查在前（403），加权限后仍是 410（旧回调端点整体停用）。
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
        # 加了权限后：旧回调端点仍 410（retired 在权限检查之后生效）
        assert response.status_code == 410, response.text
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
        project_id=_project(db).project_id,
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
        # 2026-08-18：旧 review-results 人工审核回调已停用（_retired → 410），
        # 职责分离改由自动审核 + 复核包流程承载；此处验证旧回调端点已停用。
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
        assert response.status_code == 410, response.text
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
        assert distinct_review.status_code == 410, distinct_review.text

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
        assert self_replay.status_code == 410, self_replay.text
    finally:
        settings.replenishment_beta_enabled = original

    db.expire_all()
    # 2026-08-18：旧人工审核已停用（review-results 410），不再产生 review 行
    review = db.scalar(
        select(ReplenishmentReview).where(
            ReplenishmentReview.version_id == version["version_id"]
        )
    )
    assert review is None


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
    project = _project(db, "RTEXT-0001")
    part = DimPart(pn_std="TEXT-BOUNDS-PN", description="文本边界测试件", status="active")
    db.add(part)
    db.commit()
    settings = get_settings()
    original = settings.replenishment_beta_enabled
    try:
        settings.replenishment_beta_enabled = True
        # 原子提交需 client_request_id + project_id + 有效 lines；
        # 超长 request_note 由业务层校验（400），且错误信息不反射原文
        note_response = client.post(
            "/api/replenishment-beta/applications",
            json={
                "client_request_id": "beta-text-bounds-crid",
                "project_id": project.project_id,
                "request_note": secret_note,
                "lines": [{"part_id": part.id, "quantity": 1, "special_note": None}],
            },
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
    # 2026-08-18 自动审核口径：半年价格窗 = 182 天（LOOKBACK_DAYS）
    assert item["price_window"]["days"] == 182


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
        db,
        username=user.username,
        warehouse="北京前置库",
        request_note=None,
        project_id=_project(db).project_id,
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
        db,
        username=user.username,
        warehouse="北京前置库",
        request_note=None,
        project_id=_project(db).project_id,
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

    # 2026-08-18：旧人工审核回调已停用——提交后 status 不可变（guard 拒绝
    # submitted → approved），record_review 一律抛错，不产生 review 记录。
    for _ in range(2):
        with pytest.raises(Exception):
            callback()


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
        project_id=_project(db).project_id,
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
        "screening",
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
    # 2026-08-18：旧人工审核回调已停用——提交后 status 不可变（guard 拒绝
    # submitted 后改状态），record_review 一律抛错，不产生 review 记录。
    with pytest.raises(Exception):
        replenishment.record_review(
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
    db.rollback()

    # Review-line provenance is also enforced at the database boundary: an
    # append-only row may not point at a line from another application/version.
    foreign = replenishment.create_application(
        db,
        username=user.username,
        warehouse="北京前置库",
        request_note=None,
        project_id=_project(db).project_id,
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
    # 2026-08-18：旧人工审核已停用（record_review 被 guard 拒），review 行不再由
    # 服务创建；此处直接构造一条 review 行，验证「review_line 不得指向其他
    # 版本/申请的 line」的 DB 层 guard 仍生效。
    review_id = "00000000-0000-0000-0000-000000000090"
    db.execute(
        text(
            """
            INSERT INTO replenishment_review
              (review_id, version_id, idempotency_key, payload_digest,
               approved_count, rejected_count, reviewed_by)
            VALUES
              (:review_id, :version_id, 'closed-loop-key', :digest, 1, 0, 'reviewer')
            """
        ),
        {
            "review_id": review_id,
            "version_id": submitted["versions"][0]["version_id"],
            "digest": "ab" * 32,
        },
    )
    db.commit()
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
                "review_id": review_id,
                "line_id": foreign["versions"][0]["lines"][0]["line_id"],
            },
        )
        db.commit()
    db.rollback()

    # 2026-08-18：后半段（打回重编辑/复核包导出闭环）依赖旧人工审核流程
    # （record_review/start_revision 已停用），由新流程测试单独覆盖，此处截断。
