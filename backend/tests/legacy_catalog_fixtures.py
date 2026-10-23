"""Build pre-catalog migration fixtures in the disposable pytest database only.

Current loaders correctly emit PN audit/version side effects. These older
migration scenarios model a source snapshot predating PN adoption, so discard
only those fixture side effects before exercising the real downgrade guards.
Production downgrade code and PN-specific migration tests remain untouched.
"""
from sqlalchemy import text


def pre_catalog_snapshot(db):
    db.flush()
    tables = sorted(name for name in db.scalars(text(
        "SELECT tablename FROM pg_tables WHERE schemaname=current_schema()"
    )) if name.startswith("pn_catalog_"))
    # Keep the whole catalog FK closure in the same TRUNCATE. Do not CASCADE into
    # unrelated business history; this snapshot predates all catalog adoption.
    quote = db.get_bind().dialect.identifier_preparer.quote
    if tables:
        db.execute(text("TRUNCATE " + ", ".join(quote(name) for name in tables)))
    db.expire_all()


def delete_baseline_parts(connection, *, prefix):
    # upgrade head backfills zero-version rows for legacy PN IDs. Remove only
    # pristine fixture baselines before their owning legacy rows are cleaned up.
    if connection.execute(text("SELECT to_regclass('pn_catalog_state')")).scalar() is None:
        return
    connection.execute(text("""DELETE FROM pn_catalog_state s USING dim_part p
        WHERE s.part_id=p.id AND p.pn_std LIKE :prefix
          AND s.profile_revision=0 AND s.identity_revision=0
          AND s.availability_revision=0 AND s.availability='active'
          AND s.manufacturer IS NULL AND s.field_versions='{}'::jsonb
          AND s.deleted_specs='[]'::jsonb AND s.field_provenance='{}'::jsonb
    """), {"prefix": prefix})
