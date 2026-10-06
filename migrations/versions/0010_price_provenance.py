"""Price sources, billing tiers, and one-hour cache rates."""

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE prices ADD COLUMN source_url text")
    op.execute("ALTER TABLE prices ADD COLUMN verified_at timestamptz")
    op.execute("ALTER TABLE prices ADD COLUMN meter jsonb NOT NULL DEFAULT '{}'")
    op.execute("ALTER TABLE prices ADD COLUMN cache_write_hour bigint NOT NULL DEFAULT 0")
    op.execute(
        "ALTER TABLE prices ADD CONSTRAINT prices_nonnegative CHECK "
        "(input >= 0 AND output >= 0 AND cache_read >= 0 AND cache_write >= 0 "
        "AND cache_write_hour >= 0 AND reasoning >= 0)"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE prices DROP CONSTRAINT prices_nonnegative")
    for name in ("source_url", "verified_at", "meter", "cache_write_hour"):
        op.execute("ALTER TABLE prices DROP COLUMN " + name)
