"""钉版本迁移往返、旧关联不猜版本及 ORM 零漂移。"""
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text

from tests.conftest import _alembic_cfg


_REVISION, _PREVIOUS = "b2d4f6a8c0e1", "a1c3e5f7b9d2"
_COLUMNS = {
    "presales_inquiry": "won_offer_revision_no",
    "presales_inquiry_line": "ref_offer_revision_no",
    "presales_line_response": "ref_offer_revision_no",
    "presales_offer_line_link": "offer_revision_no",
}


def test_pinned_revision_migration_roundtrip_and_zero_drift(db):
    cfg = _alembic_cfg()
    scripts = ScriptDirectory.from_config(cfg)
    # 后续迁移会接在本迁移之后：只要求迁移链单 head 且包含本迁移。
    heads = scripts.get_heads()
    assert len(heads) == 1
    assert _REVISION in {r.revision for r in scripts.walk_revisions('base', heads[0])}
    assert scripts.get_revision(_REVISION).down_revision == _PREVIOUS
    engine = db.get_bind()
    db.close()
    try:
        command.downgrade(cfg, _PREVIOUS)
        with engine.begin() as conn:
            for table, column in _COLUMNS.items():
                assert column not in {row["name"] for row in inspect(conn).get_columns(table)}
            author = conn.scalar(text(
                "INSERT INTO sys_user (username, password_hash, role) "
                "VALUES ('r7-migration', 'unused', 'sales') RETURNING id"))
            inquiry = conn.scalar(text(
                "INSERT INTO presales_inquiry (kind, inquiry_no, created_by) "
                "VALUES ('sales_inquiry', 'XJ20261026001', :author) RETURNING id"), {"author": author})
            line = conn.scalar(text(
                "INSERT INTO presales_inquiry_line (inquiry_id, line_key, seq, pn_raw, qty) "
                "VALUES (:inquiry, 'legacy', 1, 'LEGACY-PN', 1) RETURNING id"), {"inquiry": inquiry})
            offer = conn.scalar(text(
                "INSERT INTO presales_offer (inquiry_id, line_id, created_by) "
                "VALUES (:inquiry, :line, :author) RETURNING id"),
                {"inquiry": inquiry, "line": line, "author": author})
            conn.execute(text(
                "INSERT INTO presales_offer_revision (offer_id, revision_no, snapshot, actor) "
                "VALUES (:offer, 1, '{\"unit_price\": \"100.00\"}', :author)"),
                {"offer": offer, "author": author})
            conn.execute(text("UPDATE presales_inquiry SET won_offer_id = :offer WHERE id = :inquiry"),
                         {"offer": offer, "inquiry": inquiry})
            conn.execute(text("UPDATE presales_inquiry_line SET ref_offer_id = :offer WHERE id = :line"),
                         {"offer": offer, "line": line})
            conn.execute(text(
                "INSERT INTO presales_line_response (line_id, created_by, line_version, mode, ref_offer_id) "
                "VALUES (:line, :author, 1, 'reuse', :offer)"),
                {"line": line, "author": author, "offer": offer})
            conn.execute(text(
                "INSERT INTO presales_offer_line_link (line_id, offer_id) VALUES (:line, :offer)"),
                {"line": line, "offer": offer})
        command.upgrade(cfg, "head")
        command.check(cfg)
        with engine.connect() as conn:
            for table, column in _COLUMNS.items():
                columns = {row["name"]: row for row in inspect(conn).get_columns(table)}
                assert columns[column]["nullable"] is True
                assert columns[column]["default"] is None
                assert conn.scalar(text(f"SELECT count(*) FROM {table} WHERE {column} IS NULL")) == 1
            assert conn.scalar(text("SELECT snapshot FROM presales_offer_revision WHERE offer_id = :offer"),
                               {"offer": offer}) == {"unit_price": "100.00"}
        command.downgrade(cfg, _PREVIOUS)
        with engine.connect() as conn:
            for table, column in _COLUMNS.items():
                assert column not in {row["name"] for row in inspect(conn).get_columns(table)}
                assert conn.scalar(text(f"SELECT count(*) FROM {table}")) == 1
        command.upgrade(cfg, "head")
        command.check(cfg)
    finally:
        command.upgrade(cfg, "head")
