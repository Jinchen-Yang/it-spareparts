"""Build pre-catalog migration fixtures in the disposable pytest database only.

Current loaders correctly emit PN audit/version side effects. These older
migration scenarios model a source snapshot predating PN adoption, so discard
only those fixture side effects before exercising the real downgrade guards.
Production downgrade code and PN-specific migration tests remain untouched.
"""
from sqlalchemy import text


def pre_catalog_snapshot(db):
    db.flush()
    db.execute(text("TRUNCATE pn_catalog_receipt, pn_catalog_audit, pn_catalog_state"))
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
