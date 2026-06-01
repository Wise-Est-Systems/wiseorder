"""distribution pipeline schema — submissions + reply_events

Adds two tables to support the distribution_pipeline workflow:

  submissions  — one row per approved+submitted ChannelDraft. Holds the
                 external_id (HN item id, Message-Id, etc.) and external_url
                 so monitor() can look up replies.
  reply_events — one row per observed reply (HN comment, email reply, etc.).
                 Surfaced to the operator via the existing approval surface.

Revision ID: 0002_distribution
Revises: 0001_initial
Create Date: 2026-06-01
"""

from __future__ import annotations

from alembic import op


revision = "0002_distribution"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS submissions (
            id              BIGSERIAL PRIMARY KEY,
            workflow_id     BIGINT REFERENCES workflows(id) ON DELETE SET NULL,
            approval_id     BIGINT REFERENCES approvals(id) ON DELETE SET NULL,
            channel         TEXT NOT NULL,
            ask_type        TEXT NOT NULL,
            title           TEXT,
            body            TEXT NOT NULL,
            url             TEXT,
            recipient       TEXT,
            external_id     TEXT,
            external_url    TEXT,
            success         BOOLEAN NOT NULL DEFAULT FALSE,
            error           TEXT,
            submitted_at    TIMESTAMPTZ,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_submissions_channel ON submissions(channel)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_submissions_approval ON submissions(approval_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_submissions_external ON submissions(channel, external_id)"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS reply_events (
            id                       BIGSERIAL PRIMARY KEY,
            submission_id            BIGINT REFERENCES submissions(id) ON DELETE CASCADE,
            channel                  TEXT NOT NULL,
            external_submission_id   TEXT NOT NULL,
            reply_id                 TEXT NOT NULL,
            author                   TEXT,
            body                     TEXT NOT NULL,
            received_at              TIMESTAMPTZ NOT NULL,
            created_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (channel, external_submission_id, reply_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_reply_events_submission ON reply_events(submission_id)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS reply_events")
    op.execute("DROP TABLE IF EXISTS submissions")
