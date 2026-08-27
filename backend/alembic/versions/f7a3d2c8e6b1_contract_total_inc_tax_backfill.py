"""Withdraw the unsafe contract-tax backfill before it reaches new databases.

This revision was already applied to production with an unproven default tax
rate and without row-level provenance.  Its revision identifier must remain in
the chain, but fresh installs must not reproduce those writes.  Existing
installations are repaired only by the guarded, manifest-driven forward
remediation introduced after this revision.

Revision ID: f7a3d2c8e6b1
Revises: d5c1f8a3b7e2
"""

from collections.abc import Sequence

revision: str = "f7a3d2c8e6b1"
down_revision: str | None = "d5c1f8a3b7e2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Intentionally empty.  Do not infer a tax rate or mutate business data in
    # a schema migration.  Production instances that previously ran the old
    # body require the reviewed a9c4e7b2d6f1 remediation manifest.
    pass


def downgrade() -> None:
    # No schema or business data was changed by the safe revision body.
    pass
