"""Initial feedback and history schema."""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0001_initial_feedback_history_schema"
down_revision = None
branch_labels = None
depends_on = None


def _is_feedback_migration() -> bool:
    bind = op.get_bind()
    database = bind.engine.url.database or ""
    return database.endswith("feedback.db")


def upgrade() -> None:
    if _is_feedback_migration():
        op.create_table(
            "feedback_events",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("token", sa.String(length=36), nullable=False),
            sa.Column("signal", sa.String(length=32), nullable=False),
            sa.Column("event_id", sa.String(length=64), nullable=True),
            sa.Column("run_id", sa.String(length=32), nullable=False),
            sa.Column("rank", sa.Integer(), nullable=True),
            sa.Column(
                "categories_json",
                sa.Text(),
                server_default=sa.text("'[]'"),
                nullable=False,
            ),
            sa.Column("score_breakdown_json", sa.Text(), nullable=True),
            sa.Column("redirect_url", sa.Text(), nullable=True),
            sa.Column("received_at", sa.String(length=32), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            op.f("ix_feedback_events_token"),
            "feedback_events",
            ["token"],
            unique=False,
        )
        op.create_index(
            op.f("ix_feedback_events_run_id"),
            "feedback_events",
            ["run_id"],
            unique=False,
        )
        return

    op.create_table(
        "recommendation_history",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("feedback_token", sa.String(length=36), nullable=False),
        sa.Column("event_id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=32), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("score_breakdown_json", sa.Text(), nullable=False),
        sa.Column("event_title", sa.Text(), nullable=False),
        sa.Column("categories_json", sa.Text(), nullable=False),
        sa.Column("explanation", sa.Text(), nullable=False),
        sa.Column("neighborhood_context", sa.Text(), nullable=True),
        sa.Column("sellout_risk", sa.String(length=16), nullable=True),
        sa.Column("sellout_urgency_note", sa.Text(), nullable=True),
        sa.Column(
            "is_wildcard",
            sa.Boolean(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("recommended_at", sa.String(length=32), nullable=False),
        sa.Column("feedback_signal", sa.String(length=32), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("feedback_token"),
    )
    op.create_index(
        op.f("ix_recommendation_history_event_id"),
        "recommendation_history",
        ["event_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_recommendation_history_run_id"),
        "recommendation_history",
        ["run_id"],
        unique=False,
    )


def downgrade() -> None:
    if _is_feedback_migration():
        op.drop_index(op.f("ix_feedback_events_run_id"), table_name="feedback_events")
        op.drop_index(op.f("ix_feedback_events_token"), table_name="feedback_events")
        op.drop_table("feedback_events")
        return

    op.drop_index(
        op.f("ix_recommendation_history_run_id"),
        table_name="recommendation_history",
    )
    op.drop_index(
        op.f("ix_recommendation_history_event_id"),
        table_name="recommendation_history",
    )
    op.drop_table("recommendation_history")
