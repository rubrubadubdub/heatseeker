"""Admit the 'deprecated' lifecycle state in the source_definition CHECK constraint.

Revision ID: 0006
Revises: 0005
Create Date: 2026-07-11
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD = "lifecycle_status IN ('proposed','candidate','active','degraded','disabled','rejected')"
_NEW = (
    "lifecycle_status IN "
    "('proposed','candidate','active','degraded','disabled','deprecated','rejected')"
)
# The metadata naming convention prefixes ck_<table>_ onto whatever name is given.
# 0004 passed an already-prefixed name, so the DB holds a double-prefixed constraint;
# passing the single-prefixed name here lets the convention resolve to that DB name.


def upgrade() -> None:
    with op.batch_alter_table("source_definition") as batch:
        batch.drop_constraint("ck_source_definition_lifecycle_status", type_="check")
        batch.create_check_constraint("lifecycle_status", _NEW)


def downgrade() -> None:
    with op.batch_alter_table("source_definition") as batch:
        batch.drop_constraint("lifecycle_status", type_="check")
        batch.create_check_constraint("ck_source_definition_lifecycle_status", _OLD)
