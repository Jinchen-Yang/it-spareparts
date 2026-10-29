"""D-0049: keep a PN's technical update version monotonic, without a backfill.

Revision ID: a3c5e7f9b2d4
Revises: f1a3c5e7b9d2
"""
from alembic import op

revision = "a3c5e7f9b2d4"
down_revision = "f1a3c5e7b9d2"
branch_labels = None
depends_on = None

# BEFORE triggers cannot read NEW's stored generated columns. Keep this explicit
# list aligned with every ordinary dim_part column; a schema contract checks it.
# Future ordinary columns need a replacement function in their own migration.
COMPARED_COLUMNS = (
    "id", "pn_std", "pn_raw_sample", "description", "brand", "manufacturer", "summary",
    "category_major", "category_minor", "machine_or_part", "unit", "needs_review",
    "is_excluded", "exclude_reason", "status", "merged_into_id", "brand_id", "category_id",
    "data_quality_score", "reviewed_at", "master_source", "locked_fields", "category_source",
    "created_at", "updated_at",
)


def upgrade():
    old = ", ".join(f"OLD.{name}" for name in COMPARED_COLUMNS)
    new = ", ".join(f"NEW.{name}" for name in COMPARED_COLUMNS)
    op.execute(f"""
        CREATE FUNCTION itsp_dim_part_monotonic_updated_at() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF ROW({new}) IS NOT DISTINCT FROM ROW({old}) THEN
                RETURN NEW;
            END IF;
            -- Preserve the column's existing NOT NULL rejection, rather than
            -- turning an explicitly invalid NULL into a successful update.
            IF NEW.updated_at IS NULL THEN
                RETURN NEW;
            END IF;
            -- Infinity cannot be a usable strictly advancing API timestamp.
            -- Overflow at the PostgreSQL timestamp limit also fails atomically.
            IF NOT isfinite(OLD.updated_at) OR NOT isfinite(NEW.updated_at) THEN
                RAISE EXCEPTION 'PN update version must be finite'
                    USING ERRCODE = '22008';
            END IF;
            NEW.updated_at := GREATEST(clock_timestamp(),
                                      OLD.updated_at + INTERVAL '1 microsecond',
                                      NEW.updated_at);
            RETURN NEW;
        END;
        $$
    """)
    op.execute("""
        CREATE TRIGGER trg_dim_part_monotonic_updated_at
        BEFORE UPDATE ON dim_part FOR EACH ROW
        EXECUTE FUNCTION itsp_dim_part_monotonic_updated_at()
    """)


def downgrade():
    op.execute("DROP TRIGGER trg_dim_part_monotonic_updated_at ON dim_part")
    op.execute("DROP FUNCTION itsp_dim_part_monotonic_updated_at()")
