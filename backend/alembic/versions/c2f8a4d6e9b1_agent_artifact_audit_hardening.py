"""artifact lifecycle audit and JSON shape hardening

Revision ID: c2f8a4d6e9b1
Revises: b1e7c9d4f2a8
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "c2f8a4d6e9b1"
down_revision: str | None = "b1e7c9d4f2a8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CANONICAL_TEMPLATE_SHA256 = (
    "04af664a1ef445eddd3b91b55b352609b7ded62212be21bee51eede3a5400ecd"
)

# Keep the constraint text local to this revision.  c2 must repair databases that
# already executed the original b1 identity classifier; merely editing b1 would only
# fix fresh installs and leave upgraded databases on the weaker structural proof.
_ACCESS_SCOPE_CHECK_V2 = f"""
(
    jsonb_typeof(source_ids) = 'array'
    AND jsonb_typeof(access_scope) = 'object'
    AND access_scope->>'schema_version' = 'artifact-access/v2'
    AND access_scope->>'sensitivity' = sensitivity
    AND jsonb_typeof(access_scope->'required_permissions') = 'array'
    AND jsonb_typeof(access_scope->'contained_resources') = 'array'
    AND jsonb_typeof(access_scope->'contained_fields') = 'array'
    AND jsonb_typeof(access_scope->'condition') = 'object'
    AND jsonb_typeof(access_scope->'source_access_snapshots') = 'array'
    AND (
        (
            kind = 'upload'
            AND source_ids = '[]'::jsonb
            AND (
                access_scope = jsonb_build_object(
                    'schema_version', 'artifact-access/v2',
                    'policy', 'owner_only',
                    'classification', 'business_content',
                    'proof_version', 'upload-unclassified/v1',
                    'containment_status', 'unclassified',
                    'required_permissions', '[]'::jsonb,
                    'contained_resources', '[]'::jsonb,
                    'contained_fields', '[]'::jsonb,
                    'sensitivity', 'critical',
                    'row_subject', NULL,
                    'predicate_version', 'unclassified/v1',
                    'condition', jsonb_build_object('op', 'unknown'),
                    'source_access_snapshots', '[]'::jsonb,
                    'template_proof', NULL
                )
                OR access_scope = jsonb_build_object(
                    'schema_version', 'artifact-access/v2',
                    'policy', 'owner_only',
                    'classification', 'identity_only',
                    'proof_version', 'identity-template-classifier/v2',
                    'containment_status', 'classified',
                    'required_permissions', '[]'::jsonb,
                    'contained_resources', '[]'::jsonb,
                    'contained_fields', '[]'::jsonb,
                    'sensitivity', 'low',
                    'row_subject', NULL,
                    'predicate_version', 'identity-top/v1',
                    'condition', jsonb_build_object('op', 'top'),
                    'source_access_snapshots', '[]'::jsonb,
                    'template_proof', jsonb_build_object(
                        'classifier_version', 'identity-template-classifier/v2',
                        'profile_id', 'pn-replenishment-request/v1',
                        'template_id', 'pn-replenishment-request',
                        'template_version', 1,
                        'template_sha256', sha256,
                        'sheet_headers', jsonb_build_array(jsonb_build_object(
                            'sheet', '申请',
                            'headers', jsonb_build_array('PN', '数量', '备注')
                        )),
                        'safe_style_profile', 'canonical-xlsx-bytes/v1',
                        'pre_model', TRUE
                    )
                )
            )
            AND (
                access_scope->>'classification' <> 'identity_only'
                OR sha256 = '{_CANONICAL_TEMPLATE_SHA256}'
            )
        )
        OR (
            kind = 'generated'
            AND access_scope ?& ARRAY[
                'schema_version', 'policy', 'classification', 'proof_version',
                'required_permissions', 'contained_resources',
                'contained_fields', 'sensitivity', 'row_subject',
                'predicate_version', 'condition', 'source_access_snapshots'
            ]
            AND access_scope - ARRAY[
                'schema_version', 'policy', 'classification', 'proof_version',
                'required_permissions', 'contained_resources',
                'contained_fields', 'sensitivity', 'row_subject',
                'predicate_version', 'condition', 'source_access_snapshots'
            ] = '{{}}'::jsonb
            AND access_scope->>'policy' = 'provenance_guarded'
            AND access_scope->>'classification' = 'business_content'
            AND access_scope->>'proof_version' = 'source-union/v1'
            AND access_scope->'row_subject' = 'null'::jsonb
            AND access_scope->>'predicate_version' = 'source-condition-set/v1'
            AND access_scope->'condition' = '{{"op":"all_sources"}}'::jsonb
            AND jsonb_array_length(
                access_scope->'source_access_snapshots'
            ) > 0
        )
        OR (
            kind = 'generated'
            AND sensitivity = 'critical'
            AND source_ids = '[]'::jsonb
            AND access_scope = jsonb_build_object(
                'schema_version', 'artifact-access/v2',
                'policy', 'unclassified_deny',
                'classification', 'unclassified',
                'proof_version', 'legacy-generated-unproven/v1',
                'required_permissions', '[]'::jsonb,
                'contained_resources', '[]'::jsonb,
                'contained_fields', '[]'::jsonb,
                'sensitivity', 'critical',
                'row_subject', NULL,
                'predicate_version', 'unclassified/v1',
                'condition', jsonb_build_object('op', 'unknown'),
                'source_access_snapshots', '[]'::jsonb,
                'template_proof', NULL
            )
        )
    )
) IS TRUE
"""


def _v1_access_scope_check() -> str:
    """Reconstruct the original b1 constraint for a faithful c2 downgrade."""
    check = _ACCESS_SCOPE_CHECK_V2.replace(
        "'identity-template-classifier/v2'",
        "'identity-template-classifier/v1'",
    )
    check = check.replace(
        "                        'template_id', 'pn-replenishment-request',\n"
        "                        'template_version', 1,\n",
        "",
    )
    check = check.replace(
        "'safe_style_profile', 'canonical-xlsx-bytes/v1'",
        "'safe_style_profile', 'default-style-only/v1'",
    )
    check = check.replace(
        "            AND (\n"
        "                access_scope->>'classification' <> 'identity_only'\n"
        f"                OR sha256 = '{_CANONICAL_TEMPLATE_SHA256}'\n"
        "            )\n",
        "",
    )
    if (
        "identity-template-classifier/v2" in check
        or "canonical-xlsx-bytes/v1" in check
        or "'template_id'" in check
        or _CANONICAL_TEMPLATE_SHA256 in check
    ):
        raise RuntimeError("c2 cannot reconstruct the b1 access-scope constraint")
    return check


_ACCESS_SCOPE_CHECK_V1 = _v1_access_scope_check()


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("LOCK TABLE agent_artifact IN SHARE ROW EXCLUSIVE MODE")
    # SQL migrations have no signing key and therefore must never manufacture trust
    # for historical rows. NULL means pre-binding/unsealed and runtime access denies
    # it until a separately approved operator reseal workflow exists.
    op.add_column(
        "agent_artifact",
        sa.Column(
            "binding_envelope",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )
    op.create_check_constraint(
        "ck_agent_artifact_binding_object",
        "agent_artifact",
        "binding_envelope IS NULL "
        "OR jsonb_typeof(binding_envelope) = 'object'",
    )
    # Do not silently coerce corrupt metadata into an object.  Operators must repair
    # it explicitly while Artifact routes remain disabled, preserving fail-closed
    # provenance instead of manufacturing an apparently valid record.
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (
            SELECT 1 FROM agent_artifact
            WHERE jsonb_typeof(extra_meta) IS DISTINCT FROM 'object'
          ) THEN
            RAISE EXCEPTION
              'c2f8a4d6e9b1 upgrade blocked: agent_artifact.extra_meta contains non-object JSON';
          END IF;
        END
        $$;
        """
    )
    op.drop_constraint(
        "ck_agent_artifact_access_scope_v2",
        "agent_artifact",
        type_="check",
    )
    # The original b1 classifier trusted workbook structure.  Only the one exact
    # canonical byte hash may retain identity-only status.  Every other historical
    # identity claim is demoted to the ordinary unclassified upload scope.
    op.execute(
        f"""
        UPDATE agent_artifact
        SET
            extra_meta = extra_meta || jsonb_build_object(
                'legacy_identity_scope_before_c2', access_scope
            ),
            sensitivity = CASE
                WHEN sha256 = '{_CANONICAL_TEMPLATE_SHA256}' THEN 'low'
                ELSE 'critical'
            END,
            access_scope = CASE
                WHEN sha256 = '{_CANONICAL_TEMPLATE_SHA256}' THEN
                    jsonb_build_object(
                        'schema_version', 'artifact-access/v2',
                        'policy', 'owner_only',
                        'classification', 'identity_only',
                        'proof_version', 'identity-template-classifier/v2',
                        'containment_status', 'classified',
                        'required_permissions', '[]'::jsonb,
                        'contained_resources', '[]'::jsonb,
                        'contained_fields', '[]'::jsonb,
                        'sensitivity', 'low',
                        'row_subject', NULL,
                        'predicate_version', 'identity-top/v1',
                        'condition', jsonb_build_object('op', 'top'),
                        'source_access_snapshots', '[]'::jsonb,
                        'template_proof', jsonb_build_object(
                            'classifier_version', 'identity-template-classifier/v2',
                            'profile_id', 'pn-replenishment-request/v1',
                            'template_id', 'pn-replenishment-request',
                            'template_version', 1,
                            'template_sha256', sha256,
                            'sheet_headers', jsonb_build_array(jsonb_build_object(
                                'sheet', '申请',
                                'headers', jsonb_build_array('PN', '数量', '备注')
                            )),
                            'safe_style_profile', 'canonical-xlsx-bytes/v1',
                            'pre_model', TRUE
                        )
                    )
                ELSE
                    jsonb_build_object(
                        'schema_version', 'artifact-access/v2',
                        'policy', 'owner_only',
                        'classification', 'business_content',
                        'proof_version', 'upload-unclassified/v1',
                        'containment_status', 'unclassified',
                        'required_permissions', '[]'::jsonb,
                        'contained_resources', '[]'::jsonb,
                        'contained_fields', '[]'::jsonb,
                        'sensitivity', 'critical',
                        'row_subject', NULL,
                        'predicate_version', 'unclassified/v1',
                        'condition', jsonb_build_object('op', 'unknown'),
                        'source_access_snapshots', '[]'::jsonb,
                        'template_proof', NULL
                    )
            END
        WHERE kind = 'upload'
          AND access_scope->>'classification' = 'identity_only'
        """
    )
    op.create_check_constraint(
        "ck_agent_artifact_access_scope_v2",
        "agent_artifact",
        _ACCESS_SCOPE_CHECK_V2,
    )
    op.create_check_constraint(
        "ck_agent_artifact_extra_meta_object",
        "agent_artifact",
        "jsonb_typeof(extra_meta) = 'object'",
    )
    op.create_check_constraint(
        "ck_agent_artifact_json_member_types",
        "agent_artifact",
        """
        NOT jsonb_path_exists(source_ids, '$[*] ? (@.type() != "string")')
        AND NOT jsonb_path_exists(
            access_scope->'required_permissions',
            '$[*] ? (@.type() != "string")'
        )
        AND NOT jsonb_path_exists(
            access_scope->'contained_resources',
            '$[*] ? (@.type() != "string")'
        )
        AND NOT jsonb_path_exists(
            access_scope->'contained_fields',
            '$[*] ? (@.type() != "string")'
        )
        AND NOT jsonb_path_exists(
            access_scope->'source_access_snapshots',
            '$[*] ? (@.type() != "object")'
        )
        """,
    )
    op.create_table(
        "agent_artifact_audit",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("artifact_id", postgresql.UUID(as_uuid=False), nullable=True),
        sa.Column("decision_key", sa.String(length=64), nullable=True),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column("from_status", sa.String(length=16), nullable=True),
        sa.Column("to_status", sa.String(length=16), nullable=True),
        sa.Column("actor", sa.String(length=64), nullable=False),
        sa.Column(
            "detail",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "jsonb_typeof(detail) = 'object'",
            name="ck_agent_artifact_audit_detail_object",
        ),
        sa.CheckConstraint(
            "char_length(btrim(action)) > 0",
            name="ck_agent_artifact_audit_action",
        ),
        sa.CheckConstraint(
            "char_length(btrim(outcome)) > 0",
            name="ck_agent_artifact_audit_outcome",
        ),
        sa.CheckConstraint(
            "char_length(btrim(actor)) > 0",
            name="ck_agent_artifact_audit_actor",
        ),
        sa.CheckConstraint(
            "decision_key IS NULL OR decision_key ~ '^[0-9a-f]{64}$'",
            name="ck_agent_artifact_audit_decision_key",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "decision_key",
            "outcome",
            name="uq_agent_artifact_audit_decision_outcome",
        ),
    )
    op.create_index(
        "ix_agent_artifact_audit_artifact_time",
        "agent_artifact_audit",
        ["artifact_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_agent_artifact_audit_action_time",
        "agent_artifact_audit",
        ["action", "created_at"],
        unique=False,
    )
    op.execute(
        """
        CREATE FUNCTION guard_agent_artifact_audit_append_only()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
          RAISE EXCEPTION
            'agent_artifact_audit is append-only: % is forbidden', TG_OP;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_agent_artifact_audit_no_update_delete
        BEFORE UPDATE OR DELETE ON agent_artifact_audit
        FOR EACH ROW EXECUTE FUNCTION guard_agent_artifact_audit_append_only()
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_agent_artifact_audit_no_truncate
        BEFORE TRUNCATE ON agent_artifact_audit
        FOR EACH STATEMENT EXECUTE FUNCTION guard_agent_artifact_audit_append_only()
        """
    )
    op.execute(
        """
        CREATE FUNCTION guard_agent_artifact_status_transition()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
          IF NEW.status IS DISTINCT FROM OLD.status THEN
            IF NOT (
              (OLD.status = 'prepared' AND NEW.status IN ('validating', 'failed'))
              OR (OLD.status = 'validating' AND NEW.status IN ('ready', 'failed'))
              OR (OLD.status = 'ready' AND NEW.status = 'expired')
            ) THEN
              RAISE EXCEPTION 'illegal agent_artifact status transition: % -> %',
                OLD.status, NEW.status;
            END IF;
            IF NEW.binding_envelope IS NOT DISTINCT FROM OLD.binding_envelope THEN
              RAISE EXCEPTION
                'agent_artifact status transition requires a new aggregate binding';
            END IF;
          END IF;
          RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_agent_artifact_status_transition
        BEFORE UPDATE OF status ON agent_artifact
        FOR EACH ROW EXECUTE FUNCTION guard_agent_artifact_status_transition()
        """
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    # Lock the evidence table in the same transaction as the emptiness decision so
    # a concurrent writer cannot commit between the check and DROP TABLE.
    op.execute(
        "LOCK TABLE agent_artifact, agent_artifact_audit "
        "IN ACCESS EXCLUSIVE MODE"
    )
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM agent_artifact_audit) THEN
            RAISE EXCEPTION
              'c2f8a4d6e9b1 downgrade blocked: durable audit history exists';
          END IF;
        END
        $$;
        """
    )
    op.execute(
        "DROP TRIGGER trg_agent_artifact_status_transition ON agent_artifact"
    )
    op.execute("DROP FUNCTION guard_agent_artifact_status_transition()")
    op.execute(
        "DROP TRIGGER trg_agent_artifact_audit_no_truncate ON agent_artifact_audit"
    )
    op.execute(
        "DROP TRIGGER trg_agent_artifact_audit_no_update_delete "
        "ON agent_artifact_audit"
    )
    op.execute("DROP FUNCTION guard_agent_artifact_audit_append_only()")
    op.drop_constraint(
        "ck_agent_artifact_access_scope_v2",
        "agent_artifact",
        type_="check",
    )
    # Restore the exact b1 schema.  Every v2 identity row is already constrained to
    # the one canonical hash, so this proof-version projection cannot broaden the set
    # of rows that were trusted while c2 was active.
    op.execute(
        """
        UPDATE agent_artifact
        SET access_scope = jsonb_build_object(
                'schema_version', 'artifact-access/v2',
                'policy', 'owner_only',
                'classification', 'identity_only',
                'proof_version', 'identity-template-classifier/v1',
                'containment_status', 'classified',
                'required_permissions', '[]'::jsonb,
                'contained_resources', '[]'::jsonb,
                'contained_fields', '[]'::jsonb,
                'sensitivity', 'low',
                'row_subject', NULL,
                'predicate_version', 'identity-top/v1',
                'condition', jsonb_build_object('op', 'top'),
                'source_access_snapshots', '[]'::jsonb,
                'template_proof', jsonb_build_object(
                    'classifier_version', 'identity-template-classifier/v1',
                    'profile_id', 'pn-replenishment-request/v1',
                    'template_sha256', sha256,
                    'sheet_headers', jsonb_build_array(jsonb_build_object(
                        'sheet', '申请',
                        'headers', jsonb_build_array('PN', '数量', '备注')
                    )),
                    'safe_style_profile', 'default-style-only/v1',
                    'pre_model', TRUE
                )
            )
        WHERE kind = 'upload'
          AND access_scope->>'classification' = 'identity_only'
        """
    )
    op.create_check_constraint(
        "ck_agent_artifact_access_scope_v2",
        "agent_artifact",
        _ACCESS_SCOPE_CHECK_V1,
    )
    op.drop_index(
        "ix_agent_artifact_audit_action_time",
        table_name="agent_artifact_audit",
    )
    op.drop_index(
        "ix_agent_artifact_audit_artifact_time",
        table_name="agent_artifact_audit",
    )
    op.drop_table("agent_artifact_audit")
    op.drop_constraint(
        "ck_agent_artifact_json_member_types",
        "agent_artifact",
        type_="check",
    )
    op.drop_constraint(
        "ck_agent_artifact_extra_meta_object",
        "agent_artifact",
        type_="check",
    )
    op.drop_constraint(
        "ck_agent_artifact_binding_object",
        "agent_artifact",
        type_="check",
    )
    op.drop_column("agent_artifact", "binding_envelope")
