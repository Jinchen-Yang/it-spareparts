"""Shared media metadata, immutable assets and durable deletion/claim state.

Revision ID: a3c6e9f2b5d8
Revises: f2b5d8e1a4c7
"""

from alembic import op
import sqlalchemy as sa

revision = "a3c6e9f2b5d8"
down_revision = "f2b5d8e1a4c7"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "CREATE TABLE pn_media_asset (\n\tid UUID NOT NULL, \n\tobject_key VARCHAR(1024) NOT NULL, \n\tsha256 VARCHAR(64) NOT NULL, \n\tetag VARCHAR(256), \n\tverification_status VARCHAR(24) DEFAULT 'cloud_verified' NOT NULL, \n\tsize_bytes BIGINT NOT NULL, \n\tcontent_type VARCHAR(128) NOT NULL, \n\twidth INTEGER NOT NULL, \n\theight INTEGER NOT NULL, \n\towner_principal_id VARCHAR(256) NOT NULL, \n\tsource_namespace VARCHAR(64) NOT NULL, \n\tsource_id VARCHAR(256) NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tPRIMARY KEY (id), \n\tCONSTRAINT uq_pn_media_asset_source UNIQUE (source_namespace, source_id), \n\tCONSTRAINT ck_pn_media_asset_verification CHECK (verification_status IN ('legacy_metadata','cloud_verified')), \n\tCONSTRAINT ck_pn_media_asset_verified_etag CHECK (verification_status <> 'cloud_verified' OR etag IS NOT NULL), \n\tCONSTRAINT ck_pn_media_asset_dimensions CHECK (size_bytes > 0 AND width > 0 AND height > 0), \n\tUNIQUE (object_key)\n)"
    )
    op.execute("CREATE INDEX ix_pn_media_asset_sha256 ON pn_media_asset (sha256)")
    op.execute(
        "CREATE TABLE pn_media_inbox_group (\n\tid UUID NOT NULL, \n\towner_principal_id VARCHAR(256) NOT NULL, \n\tclient_id VARCHAR(128) NOT NULL, \n\tclient_group_id UUID NOT NULL, \n\tcapture_mode VARCHAR(24) NOT NULL, \n\trevision BIGINT DEFAULT '1' NOT NULL, \n\tPRIMARY KEY (id), \n\tCONSTRAINT uq_pn_media_inbox_group_client UNIQUE (owner_principal_id, client_id, client_group_id)\n)"
    )
    op.execute(
        "CREATE TABLE pn_media_derivative (\n\tid UUID NOT NULL, \n\tasset_id UUID NOT NULL, \n\trecipe_id VARCHAR(64) NOT NULL, \n\tobject_key VARCHAR(1024) NOT NULL, \n\tstatus VARCHAR(16) DEFAULT 'pending' NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tPRIMARY KEY (id), \n\tCONSTRAINT uq_pn_media_derivative_recipe UNIQUE (asset_id, recipe_id), \n\tCONSTRAINT ck_pn_media_derivative_status CHECK (status IN ('pending','ready','failed')), \n\tFOREIGN KEY(asset_id) REFERENCES pn_media_asset (id)\n)"
    )
    op.execute(
        "CREATE TABLE pn_media_entry (\n\tid UUID NOT NULL, \n\tasset_id UUID NOT NULL, \n\torigin_part_id INTEGER, \n\torigin_namespace VARCHAR(64) NOT NULL, \n\torigin_id VARCHAR(256) NOT NULL, \n\tcontent_hash VARCHAR(64) NOT NULL, \n\tpurpose VARCHAR(16) DEFAULT 'common' NOT NULL, \n\tstatus VARCHAR(16) DEFAULT 'gallery' NOT NULL, \n\trevision BIGINT DEFAULT '1' NOT NULL, \n\tgeneration BIGINT DEFAULT '1' NOT NULL, \n\tsource JSONB DEFAULT '{}'::jsonb NOT NULL, \n\tdeleted_at TIMESTAMP WITH TIME ZONE, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tPRIMARY KEY (id), \n\tCONSTRAINT uq_pn_media_entry_origin_hash_purpose UNIQUE (origin_namespace, origin_id, content_hash, purpose), \n\tCONSTRAINT ck_pn_media_entry_purpose CHECK (purpose IN ('common','defect')), \n\tCONSTRAINT ck_pn_media_entry_status CHECK (status IN ('gallery','candidate','processing','deleted')), \n\tCONSTRAINT ck_pn_media_entry_revision CHECK (revision > 0 AND generation > 0), \n\tFOREIGN KEY(asset_id) REFERENCES pn_media_asset (id), \n\tFOREIGN KEY(origin_part_id) REFERENCES dim_part (id)\n)"
    )
    op.execute(
        "CREATE INDEX ix_pn_media_entry_part_hash_purpose ON pn_media_entry (origin_part_id, content_hash, purpose)"
    )
    op.execute("CREATE INDEX ix_pn_media_entry_asset_id ON pn_media_entry (asset_id)")
    op.execute(
        "CREATE INDEX ix_pn_media_entry_origin_part_id ON pn_media_entry (origin_part_id)"
    )
    op.execute(
        "CREATE TABLE pn_media_gallery_group (\n\tid UUID NOT NULL, \n\tcanonical_part_id INTEGER, \n\trevision BIGINT DEFAULT '0' NOT NULL, \n\tprimary_entry_id UUID, \n\tordered_entry_ids JSONB DEFAULT '[]'::jsonb NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (canonical_part_id), \n\tFOREIGN KEY(canonical_part_id) REFERENCES dim_part (id)\n)"
    )
    op.execute(
        "CREATE TABLE pn_media_gallery_member (\n\tpart_id INTEGER NOT NULL, \n\tgroup_id UUID NOT NULL, \n\tPRIMARY KEY (part_id), \n\tFOREIGN KEY(part_id) REFERENCES dim_part (id), \n\tFOREIGN KEY(group_id) REFERENCES pn_media_gallery_group (id)\n)"
    )
    op.execute(
        "CREATE INDEX ix_pn_media_gallery_member_group_id ON pn_media_gallery_member (group_id)"
    )
    op.execute(
        "CREATE TABLE pn_media_suppression (\n\tid UUID NOT NULL, \n\tgroup_id UUID NOT NULL, \n\tcontent_hash VARCHAR(64) NOT NULL, \n\tactive BOOLEAN DEFAULT 'true' NOT NULL, \n\trevision BIGINT DEFAULT '1' NOT NULL, \n\treason VARCHAR(2000) NOT NULL, \n\tPRIMARY KEY (id), \n\tCONSTRAINT uq_pn_media_suppression_group_hash UNIQUE (group_id, content_hash), \n\tFOREIGN KEY(group_id) REFERENCES pn_media_gallery_group (id)\n)"
    )
    op.execute(
        "CREATE TABLE pn_media_upload (\n\tid UUID NOT NULL, \n\towner_principal_id VARCHAR(256) NOT NULL, \n\tclient_id VARCHAR(128) NOT NULL, \n\tclient_request_id UUID NOT NULL, \n\tpart_id INTEGER, \n\tidentity_revision BIGINT DEFAULT '0' NOT NULL, \n\tpurpose VARCHAR(16) DEFAULT 'common' NOT NULL, \n\tfilename VARCHAR(512) NOT NULL, \n\tdeclared_bytes BIGINT NOT NULL, \n\tcontent_type VARCHAR(128) NOT NULL, \n\tstaging_key VARCHAR(1024) NOT NULL, \n\tstatus VARCHAR(24) DEFAULT 'issued' NOT NULL, \n\trevision BIGINT DEFAULT '1' NOT NULL, \n\tgeneration BIGINT DEFAULT '1' NOT NULL, \n\tasset_id UUID, \n\tentry_id UUID, \n\tjob_id UUID, \n\terror_code VARCHAR(128), \n\texpires_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tPRIMARY KEY (id), \n\tCONSTRAINT uq_pn_media_upload_client_request UNIQUE (owner_principal_id, client_id, client_request_id), \n\tCONSTRAINT ck_pn_media_upload_status CHECK (status IN ('issued','verifying','ready','committed','cancelled','failed')), \n\tCONSTRAINT ck_pn_media_upload_bytes CHECK (declared_bytes > 0 AND declared_bytes <= 20971520), \n\tFOREIGN KEY(part_id) REFERENCES dim_part (id), \n\tUNIQUE (staging_key), \n\tFOREIGN KEY(asset_id) REFERENCES pn_media_asset (id), \n\tFOREIGN KEY(entry_id) REFERENCES pn_media_entry (id)\n)"
    )
    op.execute(
        "CREATE TABLE pn_media_inbox (\n\tid UUID NOT NULL, \n\tupload_id UUID NOT NULL, \n\tasset_id UUID, \n\tgroup_id UUID, \n\tposition INTEGER DEFAULT '0' NOT NULL, \n\towner_principal_id VARCHAR(256) NOT NULL, \n\tstatus VARCHAR(24) DEFAULT 'receiving' NOT NULL, \n\trevision BIGINT DEFAULT '1' NOT NULL, \n\tgeneration BIGINT DEFAULT '1' NOT NULL, \n\tclaim_revision BIGINT DEFAULT '0' NOT NULL, \n\tclaimed_by VARCHAR(256), \n\tclaim_expires_at TIMESTAMP WITH TIME ZONE, \n\tassignment_key UUID, \n\tpart_id INTEGER, \n\tentry_id UUID, \n\tdeleted_at TIMESTAMP WITH TIME ZONE, \n\tPRIMARY KEY (id), \n\tCONSTRAINT ck_pn_media_inbox_status CHECK (status IN ('receiving','ready','assigned','failed','deleted')), \n\tUNIQUE (upload_id), \n\tFOREIGN KEY(upload_id) REFERENCES pn_media_upload (id), \n\tFOREIGN KEY(asset_id) REFERENCES pn_media_asset (id), \n\tFOREIGN KEY(group_id) REFERENCES pn_media_inbox_group (id), \n\tUNIQUE (assignment_key), \n\tFOREIGN KEY(part_id) REFERENCES dim_part (id), \n\tFOREIGN KEY(entry_id) REFERENCES pn_media_entry (id)\n)"
    )
    op.execute(
        "CREATE INDEX ix_pn_media_inbox_discovery ON pn_media_inbox (status, id)"
    )
    op.execute("CREATE INDEX ix_pn_media_inbox_group_id ON pn_media_inbox (group_id)")
    op.execute(
        "CREATE INDEX ix_pn_media_inbox_owner_principal_id ON pn_media_inbox (owner_principal_id)"
    )
    op.execute(
        "CREATE TABLE pn_media_multipart (\n\tupload_id UUID NOT NULL, \n\tstorage_upload_id VARCHAR(1024), \n\tstate VARCHAR(24) DEFAULT 'initializing' NOT NULL, \n\tpart_size INTEGER DEFAULT '1048576' NOT NULL, \n\tparts JSONB DEFAULT '[]'::jsonb NOT NULL, \n\toperation_token UUID NOT NULL, \n\trevision BIGINT DEFAULT '1' NOT NULL, \n\taborted_at TIMESTAMP WITH TIME ZONE, \n\tPRIMARY KEY (upload_id), \n\tCONSTRAINT ck_pn_media_multipart_state CHECK (state IN ('initializing','uploading','completing','completed','cancelled')), \n\tFOREIGN KEY(upload_id) REFERENCES pn_media_upload (id)\n)"
    )
    op.execute("""CREATE FUNCTION pn_media_asset_immutable() RETURNS trigger AS $$
      BEGIN RAISE EXCEPTION 'PN media assets are immutable' USING ERRCODE = '55000'; END;
      $$ LANGUAGE plpgsql""")
    op.execute("""CREATE TRIGGER pn_media_asset_no_mutation BEFORE UPDATE OR DELETE
      ON pn_media_asset FOR EACH ROW EXECUTE FUNCTION pn_media_asset_immutable()""")


def downgrade():
    tables = [
        "pn_media_asset",
        "pn_media_inbox_group",
        "pn_media_derivative",
        "pn_media_entry",
        "pn_media_gallery_group",
        "pn_media_gallery_member",
        "pn_media_suppression",
        "pn_media_upload",
        "pn_media_inbox",
        "pn_media_multipart",
    ]
    if any(
        op.get_bind()
        .execute(sa.text(f"SELECT EXISTS (SELECT 1 FROM {name})"))
        .scalar_one()
        for name in tables
    ):
        raise RuntimeError(
            "Shared media contains adopted data; explicitly reconcile before downgrade"
        )
    op.execute("DROP TRIGGER pn_media_asset_no_mutation ON pn_media_asset")
    op.execute("DROP FUNCTION pn_media_asset_immutable()")
    for name in reversed(tables):
        op.drop_table(name)
