"""售前结构、只追加快照及权限默认值的数据库契约。"""
import pytest
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text
from sqlalchemy.exc import DBAPIError

from app import config, permissions
from app.models.presales import PresalesInquiry, PresalesInquiryLine, PresalesOffer, PresalesOfferRevision
from app.models.system import SysUser
from tests.conftest import _alembic_cfg

_REVISION, _PREVIOUS = "f9a1c3e5b7d9", "e7a9c1d3f5b7"
_KEYS = {
    "page_presales", "action_presales_inquire", "action_presales_quote",
    "action_presales_offer", "action_presales_bind_part", "action_presales_supervise",
}
_EXPECTED = {
    "admin": _KEYS,
    "boss": _KEYS,
    "sales": {"page_presales", "action_presales_inquire"},
    "purchaser": {"page_presales", "action_presales_quote", "action_presales_offer", "action_presales_bind_part"},
    "readonly": set(), "maintenance_manager": set(), "guest": set(),
}


def test_presales_permission_registration_and_frozen_defaults():
    """前端权限矩阵动态消费后端元数据；六键必须全部有中文说明和依赖。"""
    assert _KEYS <= set(permissions.ALL_KEYS)
    grouped = [key for group in permissions.UI_GROUPS for key in group["keys"]]
    for key in _KEYS:
        assert grouped.count(key) == 1
        assert permissions.LABELS[key]
        assert all(permissions.PERMISSION_META[key].get(field) for field in (
            "label", "summary", "can", "cannot", "typical", "sensitivity", "risk",
        ))
        if key.startswith("action_"):
            assert permissions.ACTION_PAGE_DEPENDENCIES[key] == "page_presales"
    for role, expected in _EXPECTED.items():
        actual = permissions.effective(role, None)
        assert {key for key in _KEYS if actual[key]} == expected, role
    assert permissions.ACTION_ADDITIONAL_PAGE_DEPENDENCIES["action_presales_bind_part"] == "page_master_data"
    assert "action_presales_supervise" in permissions.HIGH_RISK_KEYS
    assert {"supplier_id", "supplier_name", "source_key"} <= set(config.FIELD_GROUPS["supplier_info"])


def test_presales_migration_single_head_upgrade_downgrade_and_zero_drift(db):
    # conftest 为本 pytest 进程分配独占数据库；此处不操作 ENV 中的共享基库。
    cfg = _alembic_cfg()
    scripts = ScriptDirectory.from_config(cfg)
    assert scripts.get_revision(_REVISION).down_revision == _PREVIOUS
    heads = scripts.get_heads()
    assert len(heads) == 1
    assert _REVISION in {revision.revision for revision in scripts.walk_revisions("base", heads[0])}
    engine = db.get_bind()
    db.close()
    try:
        command.downgrade(cfg, _PREVIOUS)
        with engine.connect() as conn:
            assert not {
                "presales_inquiry", "presales_inquiry_line", "presales_offer",
                "presales_offer_revision", "offer_price_daily",
            } & set(inspect(conn).get_table_names())
            assert conn.scalar(text("SELECT to_regprocedure('reject_presales_offer_revision_mutation()')")) is None
            for table, column in (("sys_role_template", "permissions"),
                                  ("sys_user", "template_perms"), ("sys_user", "permissions"),
                                  ("sys_user", "perm_overrides")):
                for key in _KEYS:
                    assert conn.scalar(text(f"SELECT count(*) FROM {table} WHERE {column} ? :key"), {"key": key}) == 0
        # 在降级后种存量快照，确保默认值确实由 upgrade 写入，而非 fixture 预置。
        for role in _EXPECTED.keys() - {"guest"}:
            db.add(SysUser(
                username=f"presales-migration-{role}", role=role, password_hash="unused",
                template_perms={"page_parts": True}, permissions={"page_parts": True},
                perm_overrides={"page_inventory": False},
            ))
        db.commit()
        db.close()
        command.upgrade(cfg, "head")
        command.check(cfg)
        with engine.connect() as conn:
            templates = conn.execute(text("SELECT code, permissions FROM sys_role_template")).all()
            for role, graph in templates:
                assert {key for key in _KEYS if graph[key]} == _EXPECTED.get(role, set())
            accounts = conn.execute(text(
                "SELECT role, template_perms, permissions, perm_overrides FROM sys_user "
                "WHERE username LIKE 'presales-migration-%'"
            )).all()
            assert len(accounts) == 6
            for role, snapshot, legacy, overrides in accounts:
                assert {key for key in _KEYS if snapshot[key]} == _EXPECTED[role]
                assert {key for key in _KEYS if legacy[key]} == _EXPECTED[role]
                assert overrides == {"page_inventory": False}
                assert snapshot["page_parts"] is True
            inspector = inspect(conn)
            assert {tuple(fk["constrained_columns"]) for fk in inspector.get_foreign_keys("presales_inquiry")} >= {("won_offer_id",)}
            assert {tuple(fk["constrained_columns"]) for fk in inspector.get_foreign_keys("presales_inquiry_line")} >= {("ref_offer_id",)}
            columns = {column["name"]: column for column in inspector.get_columns("presales_offer")}
            assert (columns["unit_price"]["type"].precision, columns["unit_price"]["type"].scale) == (14, 2)
    finally:
        command.upgrade(cfg, "head")


@pytest.mark.parametrize("mutation", [
    "UPDATE presales_offer_revision SET reason = 'changed' WHERE id = :id",
    "DELETE FROM presales_offer_revision WHERE id = :id",
])
def test_presales_offer_revision_is_append_only(db, mutation):
    actor = SysUser(username="presales-schema-author", role="purchaser", password_hash="unused")
    db.add(actor)
    db.flush()
    inquiry = PresalesInquiry(kind="proactive_offer", inquiry_no="BJ20261025001", created_by=actor.id)
    db.add(inquiry)
    db.flush()
    line = PresalesInquiryLine(inquiry_id=inquiry.id, line_key="line-a", seq=1, pn_raw="RAW-PN", qty=1)
    db.add(line)
    db.flush()
    offer = PresalesOffer(inquiry_id=inquiry.id, line_id=line.id, no_stock=True, created_by=actor.id)
    db.add(offer)
    db.flush()
    revision = PresalesOfferRevision(offer_id=offer.id, revision_no=1, snapshot={"no_stock": True}, actor=actor.id)
    db.add(revision)
    db.flush()
    with pytest.raises(DBAPIError, match="presales_offer_revision is append-only"):
        with db.begin_nested():
            db.execute(text(mutation), {"id": revision.id})
    db.expire(revision)
    assert revision.snapshot == {"no_stock": True}
    assert revision.reason is None
