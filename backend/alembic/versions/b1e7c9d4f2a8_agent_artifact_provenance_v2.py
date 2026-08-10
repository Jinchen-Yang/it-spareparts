"""agent artifact provenance scope v2

Revision ID: b1e7c9d4f2a8
Revises: ad8f6c2e1b47
"""

from collections.abc import Sequence

from alembic import op


revision: str = "b1e7c9d4f2a8"
down_revision: str | None = "ad8f6c2e1b47"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_ACCESS_SCOPE_CHECK = """
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
            ] = '{}'::jsonb
            AND access_scope->>'policy' = 'provenance_guarded'
            AND access_scope->>'classification' = 'business_content'
            AND access_scope->>'proof_version' = 'source-union/v1'
            AND access_scope->'row_subject' = 'null'::jsonb
            AND access_scope->>'predicate_version' = 'source-condition-set/v1'
            AND access_scope->'condition' = '{"op":"all_sources"}'::jsonb
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


def upgrade() -> None:
    # Serialize the one-time rewrite with publishers.  Existing v1 scopes cannot be
    # promoted to trusted provenance because they lack independently authenticated
    # source snapshots.  Uploads remain owner-only/unclassified; generated outputs
    # become explicit deny records and keep their legacy facts only as audit metadata.
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("LOCK TABLE agent_artifact IN ACCESS EXCLUSIVE MODE")
    op.execute(
        """
        UPDATE agent_artifact
        SET
            extra_meta = COALESCE(extra_meta, '{}'::jsonb) || jsonb_build_object(
                'legacy_access_scope_v1', access_scope,
                'legacy_unproven_source_ids', source_ids
            ),
            source_ids = '[]'::jsonb,
            sensitivity = 'critical',
            access_scope = CASE
                WHEN kind = 'upload' THEN jsonb_build_object(
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
                ELSE jsonb_build_object(
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
            END
        WHERE access_scope->>'schema_version' IS DISTINCT FROM 'artifact-access/v2'
        """
    )
    op.create_check_constraint(
        "ck_agent_artifact_access_scope_v2",
        "agent_artifact",
        _ACCESS_SCOPE_CHECK,
    )


def downgrade() -> None:
    # Data is intentionally not rewritten back to v1.  The previous code treats the
    # migrated generated scope as unknown/denied, while uploads remain owner-only.
    # This makes rollback non-destructive and re-upgrade idempotent.
    op.drop_constraint(
        "ck_agent_artifact_access_scope_v2",
        "agent_artifact",
        type_="check",
    )
