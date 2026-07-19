"""
Recommendation history persistence for SceneScout.

Writes curated recommendation rows to ``vol-history/history.db``. Schema is
managed by Alembic — callers must run :func:`run_migrations` before use.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Literal

from sqlalchemy import Engine, create_engine, insert, select, update
from sqlalchemy.engine import Connection

from scene_scout.db.models import recommendation_history
from scene_scout.db.urls import database_history_url
from scene_scout.history_config import (
    HARD_RECENCY_DAYS,
    SOFT_RECENCY_DAYS,
    SOFT_RECENCY_SCORE_MULTIPLIER,
)
from scene_scout.logging import get_logger
from scene_scout.models.feedback import FeedbackSignal
from scene_scout.models.history import RecommendationHistoryEntry, RecommendationRecord

_engine: Engine | None = None

RecencyPenaltyBand = Literal["none", "soft", "hard"]


class HistoryEntryNotFoundError(LookupError):
    """Raised when a history row cannot be found by feedback token."""


def reset_history_engine() -> None:
    """Clear the cached SQLAlchemy engine (for tests)."""
    global _engine
    if _engine is not None:
        _engine.dispose()
    _engine = None


def _get_engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = create_engine(database_history_url())
    return _engine


def _format_dt(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _row_to_entry(row: object) -> RecommendationHistoryEntry:
    mapping = row._mapping  # type: ignore[attr-defined]
    score_breakdown = json.loads(mapping["score_breakdown_json"])
    categories = json.loads(mapping["categories_json"])
    return RecommendationHistoryEntry(
        id=mapping["id"],
        feedback_token=mapping["feedback_token"],
        event_id=mapping["event_id"],
        run_id=mapping["run_id"],
        rank=mapping["rank"],
        score=mapping["score"],
        score_breakdown=score_breakdown,
        event_title=mapping["event_title"],
        categories=categories,
        explanation=mapping["explanation"],
        neighborhood_context=mapping["neighborhood_context"],
        sellout_risk=mapping["sellout_risk"],
        sellout_urgency_note=mapping["sellout_urgency_note"],
        is_wildcard=bool(mapping["is_wildcard"]),
        recommended_at=_parse_dt(mapping["recommended_at"]),
        feedback_signal=mapping["feedback_signal"],
    )


def _record_values(record: RecommendationRecord) -> dict[str, object]:
    return {
        "feedback_token": record.feedback_token,
        "event_id": record.event_id,
        "run_id": record.run_id,
        "rank": record.rank,
        "score": record.score,
        "score_breakdown_json": json.dumps(record.score_breakdown),
        "event_title": record.event_title,
        "categories_json": json.dumps(record.categories),
        "explanation": record.explanation,
        "neighborhood_context": record.neighborhood_context,
        "sellout_risk": record.sellout_risk,
        "sellout_urgency_note": record.sellout_urgency_note,
        "is_wildcard": record.is_wildcard,
        "recommended_at": _format_dt(record.recommended_at),
        "feedback_signal": record.feedback_signal,
    }


def write_recommendations(
    records: list[RecommendationRecord],
    *,
    conn: Connection | None = None,
    run_id: str | None = None,
) -> None:
    """Persist recommendation history rows.

    Parameters
    ----------
    records : list[RecommendationRecord]
        Curated recommendations to store.
    conn : Connection, optional
        Existing SQLAlchemy connection for tests or transactions.
    run_id : str, optional
        Pipeline run identifier for structured logging.
    """
    if not records:
        return

    values = [_record_values(record) for record in records]

    if conn is not None:
        conn.execute(insert(recommendation_history), values)
    else:
        with _get_engine().begin() as connection:
            connection.execute(insert(recommendation_history), values)

    if run_id is not None:
        logger = get_logger("history", run_id=run_id)
        logger.info(
            "Wrote recommendation history",
            data={"count": len(records), "run_id": run_id},
        )


def get_recent(
    days: int,
    *,
    now: datetime | None = None,
    conn: Connection | None = None,
) -> list[RecommendationHistoryEntry]:
    """Return recommendations sent within the last ``days`` days.

    Parameters
    ----------
    days : int
        Lookback window in days.
    now : datetime, optional
        Reference time for the query. Defaults to current UTC time.
    conn : Connection, optional
        Existing SQLAlchemy connection for tests.

    Returns
    -------
    list[RecommendationHistoryEntry]
        Matching history rows, newest first.
    """
    if days < 0:
        raise ValueError("days must be non-negative")

    reference = now or datetime.now(timezone.utc)
    cutoff = _format_dt(reference - timedelta(days=days))
    stmt = (
        select(recommendation_history)
        .where(recommendation_history.c.recommended_at >= cutoff)
        .order_by(recommendation_history.c.recommended_at.desc())
    )

    if conn is not None:
        rows = conn.execute(stmt).fetchall()
    else:
        with _get_engine().connect() as connection:
            rows = connection.execute(stmt).fetchall()

    return [_row_to_entry(row) for row in rows]


def build_recency_lookup(
    *,
    now: datetime | None = None,
    conn: Connection | None = None,
) -> dict[str, datetime]:
    """Map each event ID to its most recent ``recommended_at`` within soft recency.

    Returns
    -------
    dict[str, datetime]
        Latest recommendation timestamp per event ID (newest row wins).
    """
    lookup: dict[str, datetime] = {}
    for entry in get_recent(SOFT_RECENCY_DAYS, now=now, conn=conn):
        if entry.event_id not in lookup:
            lookup[entry.event_id] = entry.recommended_at
    return lookup


def get_last_recommended_at(
    event_id: str,
    *,
    now: datetime | None = None,
    conn: Connection | None = None,
) -> datetime | None:
    """Return the most recent recommendation time for ``event_id``, if any."""
    return build_recency_lookup(now=now, conn=conn).get(event_id)


def get_recommended_event_ids(
    days: int,
    *,
    now: datetime | None = None,
    conn: Connection | None = None,
) -> set[str]:
    """Return event IDs recommended within the last ``days`` days."""
    return {entry.event_id for entry in get_recent(days, now=now, conn=conn)}


def get_soft_recency_event_ids(
    *,
    now: datetime | None = None,
    conn: Connection | None = None,
) -> set[str]:
    """Return event IDs recommended within the soft-recency window (4 weeks)."""
    return get_recommended_event_ids(
        SOFT_RECENCY_DAYS,
        now=now,
        conn=conn,
    )


def get_hard_exclude_event_ids(
    *,
    now: datetime | None = None,
    conn: Connection | None = None,
) -> set[str]:
    """Return event IDs recommended within the hard-exclude window (2 weeks)."""
    return get_recommended_event_ids(
        HARD_RECENCY_DAYS,
        now=now,
        conn=conn,
    )


def classify_recency_penalty(
    event_id: str,
    *,
    now: datetime | None = None,
    conn: Connection | None = None,
) -> RecencyPenaltyBand:
    """Classify the recency penalty band for an event ID.

    Hard exclusion (2 weeks) takes precedence over the soft multiplier (4 weeks).
    """
    if event_id in get_hard_exclude_event_ids(now=now, conn=conn):
        return "hard"
    if event_id in get_soft_recency_event_ids(now=now, conn=conn):
        return "soft"
    return "none"


def apply_soft_recency_penalty(score: float) -> float:
    """Apply the four-week soft recency multiplier to a score."""
    return max(0.0, min(1.0, score * SOFT_RECENCY_SCORE_MULTIPLIER))


def apply_recency_penalty(
    score: float,
    event_id: str,
    *,
    now: datetime | None = None,
    conn: Connection | None = None,
) -> tuple[float, RecencyPenaltyBand]:
    """Return a score adjusted for recency and the applied penalty band."""
    band = classify_recency_penalty(event_id, now=now, conn=conn)
    if band == "soft":
        return apply_soft_recency_penalty(score), band
    return score, band


def _fetch_entry_by_feedback_token(
    feedback_token: str,
    *,
    conn: Connection,
) -> RecommendationHistoryEntry | None:
    stmt = select(recommendation_history).where(
        recommendation_history.c.feedback_token == feedback_token,
    )
    row = conn.execute(stmt).first()
    if row is None:
        return None
    return _row_to_entry(row)


def get_entry_by_feedback_token(
    feedback_token: str,
    *,
    conn: Connection | None = None,
) -> RecommendationHistoryEntry | None:
    """Return the recommendation history row for ``feedback_token``, if any."""
    if conn is not None:
        return _fetch_entry_by_feedback_token(feedback_token, conn=conn)

    with _get_engine().connect() as connection:
        return _fetch_entry_by_feedback_token(feedback_token, conn=connection)


def update_feedback(
    feedback_token: str,
    signal: FeedbackSignal,
    *,
    conn: Connection | None = None,
    run_id: str | None = None,
) -> RecommendationHistoryEntry:
    """Populate ``feedback_signal`` on the matching recommendation history row.

    Parameters
    ----------
    feedback_token : str
        UUID token identifying the recommendation row.
    signal : FeedbackSignal
        Behavioral feedback signal (``click`` or ``negative``).
    conn : Connection, optional
        Existing SQLAlchemy connection for tests or transactions.
    run_id : str, optional
        Pipeline run identifier for structured logging.

    Returns
    -------
    RecommendationHistoryEntry
        Updated history row.

    Raises
    ------
    HistoryEntryNotFoundError
        When no row matches ``feedback_token``.
    """
    stmt = (
        update(recommendation_history)
        .where(recommendation_history.c.feedback_token == feedback_token)
        .values(feedback_signal=signal)
    )

    if conn is not None:
        result = conn.execute(stmt)
        if result.rowcount == 0:
            raise HistoryEntryNotFoundError(feedback_token)
        entry = _fetch_entry_by_feedback_token(feedback_token, conn=conn)
    else:
        with _get_engine().begin() as connection:
            result = connection.execute(stmt)
            if result.rowcount == 0:
                raise HistoryEntryNotFoundError(feedback_token)
            entry = _fetch_entry_by_feedback_token(feedback_token, conn=connection)

    if entry is None:
        raise HistoryEntryNotFoundError(feedback_token)

    if run_id is not None:
        logger = get_logger("history", run_id=run_id)
        logger.info(
            "Updated recommendation feedback signal",
            data={
                "feedback_token": feedback_token,
                "event_id": entry.event_id,
                "signal": signal,
            },
        )

    return entry
