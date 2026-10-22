"""D-36 catalog-v2：先安装事务捕获，再由显式 seed 建立发布世代。

Revision ID: d4e8f2a6b0c1
Revises: c9d4f2a6b8e1
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

revision = "d4e8f2a6b0c1"
down_revision = "c9d4f2a6b8e1"
branch_labels = None
depends_on = None


def _uuid(name, **kwargs):
    return sa.Column(name, pg.UUID(as_uuid=False), **kwargs)


def _big(name, **kwargs):
    return sa.Column(name, sa.BigInteger(), **kwargs)


def _json(name, **kwargs):
    return sa.Column(name, pg.JSONB(), **kwargs)


def _stamp(name="created_at", **kwargs):
    return sa.Column(name, sa.DateTime(timezone=True), server_default=sa.func.now(), **kwargs)


def upgrade():
    # PostgreSQL 的常量 DEFAULT 同时回填既有行，不改任何源业务值或 updated_at。
    op.add_column("dim_part", _big("aggregate_revision", nullable=False, server_default="1"))
    op.add_column("dim_part", _big("identity_revision", nullable=False, server_default="1"))
    op.create_table("catalog_v2_change",
        _big("id", primary_key=True, autoincrement=True), _big("txid", nullable=False),
        sa.Column("table_name", sa.String(32), nullable=False), sa.Column("op", sa.String(1), nullable=False),
        _big("part_id"), _big("old_part_id"), _big("new_part_id"), _big("alias_id"),
        _json("old_row"), _json("new_row"), _stamp("captured_at", nullable=False),
        _uuid("published_epoch"), _big("published_sequence"),
        sa.CheckConstraint("op IN ('I', 'U', 'D')", name="ck_catalog_v2_change_op"))
    op.create_index("ix_catalog_v2_change_pending_txid", "catalog_v2_change", ["txid"], postgresql_where=sa.text("published_epoch IS NULL"))
    op.create_index("ix_catalog_v2_change_part", "catalog_v2_change", ["part_id", "txid"])
    op.create_table("catalog_v2_epoch", _uuid("id", primary_key=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="seeding"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default="true"),
        _big("watermark", nullable=False, server_default="0"),
        _big("retained_from_sequence", nullable=False, server_default="0"),
        _stamp(nullable=False), sa.Column("ready_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("status IN ('seeding', 'ready', 'stopped')", name="ck_catalog_v2_epoch_status"))
    op.create_index("uq_catalog_v2_epoch_active", "catalog_v2_epoch", ["active"], unique=True, postgresql_where=sa.text("active"))
    op.create_table("catalog_v2_projection", _uuid("epoch_id", primary_key=True),
        sa.Column("kind", sa.String(8), primary_key=True), _big("entity_id", primary_key=True),
        _big("part_id", nullable=False), _json("data", nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        _big("published_sequence", nullable=False, server_default="0"),
        sa.CheckConstraint("kind IN ('node', 'alias')", name="ck_catalog_v2_projection_kind"))
    op.create_index("ix_catalog_v2_projection_part", "catalog_v2_projection", ["epoch_id", "part_id"])
    op.create_table("catalog_v2_batch", _uuid("epoch_id", primary_key=True),
        _big("published_sequence", primary_key=True), _big("previous_sequence", nullable=False),
        _uuid("batch_id", nullable=False, unique=True), _json("transaction_groups", nullable=False),
        _json("items", nullable=False), _json("alias_moves", nullable=False),
        sa.Column("batch_hash", sa.String(64), nullable=False),
        sa.Column("payload", sa.LargeBinary(), nullable=False), _stamp(nullable=False))
    op.create_table("catalog_v2_snapshot", _uuid("snapshot_id", primary_key=True),
        sa.Column("client_id", sa.String(128), nullable=False), _uuid("request_id", nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False), _uuid("epoch_id", nullable=False),
        _big("watermark", nullable=False), _big("node_count", nullable=False), _big("alias_count", nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False), _json("items", nullable=False),
        _stamp(nullable=False), sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("client_id", "request_id", name="uq_catalog_v2_snapshot_request"))
    op.create_table("catalog_v2_payload_fragment", _uuid("epoch_id", primary_key=True),
        _uuid("payload_id", primary_key=True), sa.Column("part_no", sa.Integer(), primary_key=True),
        sa.Column("part_count", sa.Integer(), nullable=False), sa.Column("data", sa.LargeBinary(), nullable=False),
        sa.Column("part_hash", sa.String(64), nullable=False), sa.Column("payload_hash", sa.String(64), nullable=False),
        _json("context", nullable=False), _stamp(nullable=False))
    op.create_table("catalog_v2_cursor", _uuid("id", primary_key=True),
        sa.Column("client_id", sa.String(128), nullable=False), _json("context", nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False), _stamp(nullable=False))
    op.create_table("catalog_v2_alarm", _big("id", primary_key=True, autoincrement=True),
        _uuid("epoch_id"), sa.Column("code", sa.String(64), nullable=False),
        _json("details", nullable=False), _stamp(nullable=False))
    op.execute(_REVISION_FUNCTION)
    op.execute(_CAPTURE_FUNCTION)
    op.execute("CREATE TRIGGER catalog_v2_revision BEFORE INSERT OR UPDATE ON dim_part FOR EACH ROW EXECUTE FUNCTION catalog_v2_revision()")
    for table in ("dim_part", "product_specs", "part_alias"):
        op.execute(f"CREATE TRIGGER catalog_v2_capture AFTER INSERT OR UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION catalog_v2_capture()")


def downgrade():
    # 生产回滚是关闭读凭据并保留捕获；此逆迁移仅供隔离迁移验证。
    for table in ("dim_part", "product_specs", "part_alias"):
        op.execute(f"DROP TRIGGER catalog_v2_capture ON {table}")
    op.execute("DROP TRIGGER catalog_v2_revision ON dim_part")
    op.execute("DROP FUNCTION catalog_v2_capture()")
    op.execute("DROP FUNCTION catalog_v2_revision()")
    for table in ("cursor", "payload_fragment", "snapshot", "projection", "batch", "alarm", "epoch", "change"):
        op.drop_table("catalog_v2_" + table)
    op.drop_column("dim_part", "identity_revision")
    op.drop_column("dim_part", "aggregate_revision")


_REVISION_FUNCTION = r"""
CREATE FUNCTION catalog_v2_revision() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    changed boolean;
    identity_changed boolean;
BEGIN
    IF TG_OP = 'INSERT' THEN
        NEW.aggregate_revision := 1;
        NEW.identity_revision := 1;
        RETURN NEW;
    END IF;
    changed := ROW(NEW.pn_std, NEW.status, NEW.merged_into_id, NEW.needs_review,
        NEW.is_excluded, NEW.machine_or_part, NEW.description, NEW.brand,
        NEW.category_major, NEW.category_minor, NEW.unit,
        ARRAY(SELECT DISTINCT x FROM unnest(NEW.locked_fields) x ORDER BY x))
      IS DISTINCT FROM ROW(OLD.pn_std, OLD.status, OLD.merged_into_id, OLD.needs_review,
        OLD.is_excluded, OLD.machine_or_part, OLD.description, OLD.brand,
        OLD.category_major, OLD.category_minor, OLD.unit,
        ARRAY(SELECT DISTINCT x FROM unnest(OLD.locked_fields) x ORDER BY x));
    identity_changed := ROW(NEW.pn_std, NEW.status, NEW.merged_into_id)
        IS DISTINCT FROM ROW(OLD.pn_std, OLD.status, OLD.merged_into_id);
    IF pg_trigger_depth() > 1 THEN
        -- 子表捕获函数只更新 revision；任何真实源列变化仍按本规则计数。
        NEW.aggregate_revision := GREATEST(OLD.aggregate_revision, NEW.aggregate_revision);
        NEW.identity_revision := GREATEST(OLD.identity_revision, NEW.identity_revision);
    ELSE
        -- 外部 UPDATE 不拥有版本；包括旧快照 ORM/Core 写入都不得倒拨或伪推进。
        NEW.aggregate_revision := OLD.aggregate_revision;
        NEW.identity_revision := OLD.identity_revision;
    END IF;
    IF changed THEN NEW.aggregate_revision := NEW.aggregate_revision + 1; END IF;
    IF identity_changed THEN NEW.identity_revision := NEW.identity_revision + 1; END IF;
    RETURN NEW;
END $$
"""


_CAPTURE_FUNCTION = r"""
CREATE FUNCTION catalog_v2_capture() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    before_row jsonb;
    after_row jsonb;
    old_parent bigint;
    new_parent bigint;
    aid bigint;
    pid bigint;
    identity_changed boolean := false;
BEGIN
    IF TG_TABLE_NAME = 'dim_part' THEN
        IF TG_OP <> 'INSERT' THEN
            old_parent := OLD.id;
            before_row := jsonb_build_object('id', OLD.id, 'pn_std', OLD.pn_std,
                'status', OLD.status, 'merged_into_id', OLD.merged_into_id,
                'aggregate_revision', OLD.aggregate_revision, 'identity_revision', OLD.identity_revision);
        END IF;
        IF TG_OP <> 'DELETE' THEN
            new_parent := NEW.id;
            after_row := jsonb_build_object('id', NEW.id, 'pn_std', NEW.pn_std,
                'status', NEW.status, 'merged_into_id', NEW.merged_into_id,
                'aggregate_revision', NEW.aggregate_revision, 'identity_revision', NEW.identity_revision);
        END IF;
    ELSIF TG_TABLE_NAME = 'product_specs' THEN
        IF TG_OP <> 'INSERT' THEN
            old_parent := OLD.part_id;
            before_row := jsonb_build_object('id', OLD.id, 'part_id', OLD.part_id,
                'spec_key', OLD.spec_key, 'spec_value', OLD.spec_value,
                'spec_unit', OLD.spec_unit, 'numeric_value', OLD.numeric_value, 'source', OLD.source);
        END IF;
        IF TG_OP <> 'DELETE' THEN
            new_parent := NEW.part_id;
            after_row := jsonb_build_object('id', NEW.id, 'part_id', NEW.part_id,
                'spec_key', NEW.spec_key, 'spec_value', NEW.spec_value,
                'spec_unit', NEW.spec_unit, 'numeric_value', NEW.numeric_value, 'source', NEW.source);
        END IF;
    ELSE
        IF TG_OP <> 'INSERT' THEN
            old_parent := COALESCE(OLD.part_id, (SELECT id FROM dim_part WHERE pn_std = OLD.pn_std));
            aid := OLD.id;
            before_row := jsonb_build_object('id', OLD.id, 'pn_raw', OLD.pn_raw,
                'part_id', old_parent, 'pn_std', OLD.pn_std, 'status', OLD.status);
        END IF;
        IF TG_OP <> 'DELETE' THEN
            new_parent := COALESCE(NEW.part_id, (SELECT id FROM dim_part WHERE pn_std = NEW.pn_std));
            aid := NEW.id;
            after_row := jsonb_build_object('id', NEW.id, 'pn_raw', NEW.pn_raw,
                'part_id', new_parent, 'pn_std', NEW.pn_std, 'status', NEW.status);
        END IF;
        identity_changed := COALESCE(before_row->>'status' = 'active', false)
            OR COALESCE(after_row->>'status' = 'active', false);
    END IF;
    -- 审计时间、来源元信息和无变化 upsert 不构成目录变化。
    IF TG_OP = 'UPDATE' AND before_row IS NOT DISTINCT FROM after_row THEN
        RETURN NULL;
    END IF;
    IF TG_TABLE_NAME = 'dim_part' AND TG_OP = 'UPDATE' THEN
        IF ROW(OLD.status, OLD.merged_into_id) IS DISTINCT FROM ROW(NEW.status, NEW.merged_into_id) THEN
            -- Core 也可写多跳链：祖先重定向改变后代的有效身份，不能同版本换根。
            -- 只推进后代版本，不替调用者改写 merged_into_id。UNION 去重确保非法环
            -- 也有限结束；子 UPDATE 仅改 revision，不再次走此传播分支。
            FOR pid IN WITH RECURSIVE descendants(id) AS (
                SELECT id FROM dim_part WHERE merged_into_id = NEW.id
                UNION
                SELECT p.id FROM dim_part p JOIN descendants d ON p.merged_into_id = d.id
            ) SELECT id FROM descendants WHERE id <> NEW.id ORDER BY id LOOP
                UPDATE dim_part SET aggregate_revision = aggregate_revision + 1,
                    identity_revision = identity_revision + 1 WHERE id = pid;
            END LOOP;
        END IF;
    END IF;
    IF TG_TABLE_NAME <> 'dim_part' THEN
        -- 同一行换属时两父都变，按稳定 ID 顺序锁父；不改变 v1 updated_at。
        FOR pid IN SELECT DISTINCT p FROM unnest(ARRAY[old_parent, new_parent]) p
                   WHERE p IS NOT NULL ORDER BY p LOOP
            UPDATE dim_part SET aggregate_revision = aggregate_revision + 1,
                identity_revision = identity_revision + CASE WHEN identity_changed THEN 1 ELSE 0 END
            WHERE id = pid;
        END LOOP;
    END IF;
    INSERT INTO catalog_v2_change(txid, table_name, op, part_id, old_part_id,
        new_part_id, alias_id, old_row, new_row)
    VALUES (txid_current(), TG_TABLE_NAME, substr(TG_OP, 1, 1), COALESCE(new_parent, old_parent),
        old_parent, new_parent, aid, before_row, after_row);
    RETURN NULL;
END $$
"""
