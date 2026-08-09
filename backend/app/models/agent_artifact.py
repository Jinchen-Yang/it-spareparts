"""Immutable AI input/output artifact metadata."""

from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, Index, String, Uuid, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._types import TZDateTime


class AgentArtifact(Base):
    """Server-owned metadata for an immutable file stored by the Agent platform."""

    __tablename__ = "agent_artifact"

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True)
    owner_sub: Mapped[str] = mapped_column(String(64), nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    media_type: Mapped[str] = mapped_column(String(127), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(512), nullable=False, unique=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    sensitivity: Mapped[str] = mapped_column(String(16), nullable=False)
    source_ids: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    access_scope: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    extra_meta: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    created_at: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)

    __table_args__ = (
        CheckConstraint(
            "status IN ('prepared', 'validating', 'ready', 'failed', 'expired')",
            name="ck_agent_artifact_status",
        ),
        CheckConstraint(
            "kind IN ('upload', 'generated')",
            name="ck_agent_artifact_kind",
        ),
        CheckConstraint(
            "sensitivity IN ('low', 'medium', 'high', 'critical')",
            name="ck_agent_artifact_sensitivity",
        ),
        CheckConstraint("size_bytes >= 0", name="ck_agent_artifact_size"),
        CheckConstraint("char_length(sha256) = 64", name="ck_agent_artifact_sha256"),
        CheckConstraint(
            "char_length(btrim(owner_sub)) > 0",
            name="ck_agent_artifact_owner",
        ),
        CheckConstraint(
            "char_length(btrim(filename)) > 0",
            name="ck_agent_artifact_filename",
        ),
        CheckConstraint(
            "char_length(btrim(storage_key)) > 0",
            name="ck_agent_artifact_storage_key",
        ),
        CheckConstraint(
            "expires_at > created_at",
            name="ck_agent_artifact_expiry",
        ),
        CheckConstraint(
            """
            (
                jsonb_typeof(source_ids) = 'array'
                AND jsonb_typeof(access_scope) = 'object'
                AND access_scope->>'schema_version' = 'artifact-access/v2'
                AND access_scope->>'sensitivity' = sensitivity
                AND jsonb_typeof(access_scope->'required_permissions') = 'array'
                AND jsonb_typeof(access_scope->'contained_resources') = 'array'
                AND jsonb_typeof(access_scope->'contained_fields') = 'array'
                AND jsonb_typeof(access_scope->'condition') = 'object'
                AND jsonb_typeof(
                    access_scope->'source_access_snapshots'
                ) = 'array'
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
                                'proof_version',
                                    'identity-template-classifier/v1',
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
                                    'classifier_version',
                                        'identity-template-classifier/v1',
                                    'profile_id',
                                        'pn-replenishment-request/v1',
                                    'template_sha256', sha256,
                                    'sheet_headers', jsonb_build_array(
                                        jsonb_build_object(
                                            'sheet', '申请',
                                            'headers', jsonb_build_array(
                                                'PN', '数量', '备注'
                                            )
                                        )
                                    ),
                                    'safe_style_profile',
                                        'default-style-only/v1',
                                    'pre_model', TRUE
                                )
                            )
                        )
                    )
                    OR (
                        kind = 'generated'
                        AND access_scope ?& ARRAY[
                            'schema_version', 'policy', 'classification',
                            'proof_version', 'required_permissions',
                            'contained_resources', 'contained_fields',
                            'sensitivity', 'row_subject', 'predicate_version',
                            'condition', 'source_access_snapshots'
                        ]
                        AND access_scope - ARRAY[
                            'schema_version', 'policy', 'classification',
                            'proof_version', 'required_permissions',
                            'contained_resources', 'contained_fields',
                            'sensitivity', 'row_subject', 'predicate_version',
                            'condition', 'source_access_snapshots'
                        ] = '{}'::jsonb
                        AND access_scope->>'policy' = 'provenance_guarded'
                        AND access_scope->>'classification' = 'business_content'
                        AND access_scope->>'proof_version' = 'source-union/v1'
                        AND access_scope->'row_subject' = 'null'::jsonb
                        AND access_scope->>'predicate_version'
                            = 'source-condition-set/v1'
                        AND access_scope->'condition'
                            = '{"op":"all_sources"}'::jsonb
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
            """,
            name="ck_agent_artifact_access_scope_v2",
        ),
        Index("ix_agent_artifact_owner_created", "owner_sub", "created_at"),
        Index("ix_agent_artifact_status_expiry", "status", "expires_at"),
    )
