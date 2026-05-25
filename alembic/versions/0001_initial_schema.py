"""initial schema — tasks, workflows, memory, approvals

Matches migrations/001_init.sql exactly. This migration is idempotent
(uses IF NOT EXISTS) so it co-exists with the docker-entrypoint-initdb.d
bootstrap path.

Revision ID: 0001_initial
Revises:
Create Date: 2026-05-25
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # tasks
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS tasks (
            id           BIGSERIAL PRIMARY KEY,
            type         TEXT NOT NULL,
            status       TEXT NOT NULL DEFAULT 'pending',
            payload      JSONB NOT NULL DEFAULT '{}'::jsonb,
            result       JSONB,
            error        TEXT,
            workflow_id  BIGINT,
            created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            started_at   TIMESTAMPTZ,
            completed_at TIMESTAMPTZ
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_tasks_workflow ON tasks(workflow_id)")

    # workflows
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS workflows (
            id            BIGSERIAL PRIMARY KEY,
            workflow_name TEXT NOT NULL,
            task_chain    JSONB NOT NULL DEFAULT '[]'::jsonb,
            status        TEXT NOT NULL DEFAULT 'pending',
            logs          JSONB NOT NULL DEFAULT '[]'::jsonb,
            created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            completed_at  TIMESTAMPTZ
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS idx_workflows_status ON workflows(status)")

    # memory
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS memory (
            id           BIGSERIAL PRIMARY KEY,
            category     TEXT NOT NULL,
            content      TEXT NOT NULL,
            embedding_id TEXT,
            metadata     JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS idx_memory_category ON memory(category)")

    # approvals
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS approvals (
            id            BIGSERIAL PRIMARY KEY,
            workflow_id   BIGINT REFERENCES workflows(id) ON DELETE CASCADE,
            task_id       BIGINT REFERENCES tasks(id) ON DELETE SET NULL,
            summary       TEXT NOT NULL,
            outputs       JSONB NOT NULL DEFAULT '{}'::jsonb,
            affected      JSONB NOT NULL DEFAULT '[]'::jsonb,
            risk_level    TEXT NOT NULL DEFAULT 'low',
            decision      TEXT,
            delivered_via TEXT,
            created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            decided_at    TIMESTAMPTZ
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS idx_approvals_decision ON approvals(decision)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS approvals")
    op.execute("DROP TABLE IF EXISTS memory")
    op.execute("DROP TABLE IF EXISTS workflows")
    op.execute("DROP TABLE IF EXISTS tasks")
