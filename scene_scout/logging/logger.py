"""
Rich-configured logging for SceneScout agents.

Terminal output uses a color-coded ``[AGENT_NAME]`` prefix per agent.
Structured JSONL entries are written to ``vol-logs/{run_id}.jsonl`` for
production observability and the web Dev Section log viewer.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.style import Style

from scene_scout.config import vol_logs_dir

# Agent key -> (display prefix, rich color name)
AGENT_COLORS: dict[str, tuple[str, str]] = {
    "orchestrator": ("ORCHESTRATOR", "white"),
    "feed_scout": ("FEED SCOUT", "cyan"),
    "event_extraction": ("EVENT EXTRACTION", "blue"),
    "event_normalization": ("EVENT NORMALIZATION", "blue"),
    "deduplication": ("DEDUPLICATION", "blue"),
    "description_quality": ("DESCRIPTION QUALITY", "yellow"),
    "geocoding": ("GEOCODING", "yellow"),
    "talent_scout": ("TALENT SCOUT", "magenta"),
    "vibe_classifier": ("VIBE CLASSIFIER", "magenta"),
    "neighborhood_scout": ("NEIGHBORHOOD SCOUT", "magenta"),
    "user_preference": ("USER PREFERENCE", "white"),
    "ranking": ("RANKING", "green"),
    "sellout_risk": ("SELL-OUT RISK", "green"),
    "recommendation_curator": ("RECOMMENDATION CURATOR", "gold1"),
    "email_composer": ("EMAIL COMPOSER", "gold1"),
    "evaluation": ("EVALUATION", "red"),
    "cache": ("CACHE", "white", True),
    "llm": ("LLM SERVICE", "white", True),
}

_LEVEL_STYLES: dict[int, Style] = {
    logging.DEBUG: Style(dim=True),
    logging.INFO: Style(),
    logging.WARNING: Style(color="yellow"),
    logging.ERROR: Style(color="red", bold=True),
    logging.CRITICAL: Style(color="red", bold=True, reverse=True),
}

_file_locks: dict[Path, threading.Lock] = {}
_logger_cache: dict[str, AgentLogger] = {}

RUN_LOG_RETENTION_DAYS = 90
_RUN_LOG_ID_FORMAT = "%Y%m%d-%H%M%S"


def _logs_dir() -> Path:
    """Return the directory for structured JSONL run logs.

    Returns
    -------
    Path
        Resolved log directory, created if it does not exist.
    """
    return vol_logs_dir()


def _lock_for(path: Path) -> threading.Lock:
    """Return a process-wide lock for the given log file path.

    Parameters
    ----------
    path : Path
        JSONL file path.

    Returns
    -------
    threading.Lock
        Lock guarding concurrent writes to ``path``.
    """
    if path not in _file_locks:
        _file_locks[path] = threading.Lock()
    return _file_locks[path]


class AgentLogger:
    """Rich-configured logger for a single pipeline agent.

    Emits color-coded terminal output with an ``[AGENT_NAME]`` prefix and
    appends structured JSONL records when a ``run_id`` is set.

    Parameters
    ----------
    agent_name : str
        Snake-case agent identifier (e.g. ``"feed_scout"``).
    run_id : str, optional
        Pipeline run identifier for JSONL correlation. When omitted, only
        terminal output is produced until ``set_run_id()`` is called.
    """

    def __init__(self, agent_name: str, run_id: str | None = None) -> None:
        self.agent_name = agent_name
        self.run_id = run_id
        entry = AGENT_COLORS.get(
            agent_name,
            (agent_name.upper().replace("_", " "), "white"),
        )
        self._display_name = entry[0]
        self._color = entry[1]
        self._dim = entry[2] if len(entry) > 2 else False
        self._console = Console(stderr=True, force_terminal=True)
        self._level = logging.INFO

    @property
    def display_name(self) -> str:
        """Human-readable agent label used in the terminal prefix."""
        return self._display_name

    @property
    def color(self) -> str:
        """Rich color name assigned to this agent."""
        if self._dim:
            return "dim white"
        return self._color

    def set_run_id(self, run_id: str) -> None:
        """Attach or update the pipeline run identifier for JSONL output.

        Parameters
        ----------
        run_id : str
            Pipeline run identifier (e.g. ``"20250606-143022"``).
        """
        self.run_id = run_id

    def set_level(self, level: int) -> None:
        """Set the minimum log level for terminal and JSONL output.

        Parameters
        ----------
        level : int
            Standard ``logging`` level constant (e.g. ``logging.DEBUG``).
        """
        self._level = level

    def debug(
        self, message: str, *args: Any, data: dict[str, Any] | None = None
    ) -> None:
        """Log a DEBUG-level message."""
        self._log(logging.DEBUG, message, *args, data=data)

    def info(
        self, message: str, *args: Any, data: dict[str, Any] | None = None
    ) -> None:
        """Log an INFO-level message."""
        self._log(logging.INFO, message, *args, data=data)

    def warning(
        self, message: str, *args: Any, data: dict[str, Any] | None = None
    ) -> None:
        """Log a WARNING-level message."""
        self._log(logging.WARNING, message, *args, data=data)

    def error(
        self, message: str, *args: Any, data: dict[str, Any] | None = None
    ) -> None:
        """Log an ERROR-level message."""
        self._log(logging.ERROR, message, *args, data=data)

    def _log(
        self,
        level: int,
        message: str,
        *args: Any,
        data: dict[str, Any] | None = None,
    ) -> None:
        if level < self._level:
            return

        formatted = message % args if args else message
        prefix = f"[{self._display_name}]"
        agent_style = Style(color=self._color, bold=not self._dim, dim=self._dim)
        level_style = _LEVEL_STYLES.get(level, Style())

        self._console.print(
            prefix,
            style=agent_style,
            end=" ",
            highlight=False,
        )
        self._console.print(formatted, style=level_style, highlight=False)

        if self.run_id is not None:
            self._write_jsonl(level, formatted, data)

    def _write_jsonl(
        self,
        level: int,
        message: str,
        data: dict[str, Any] | None,
    ) -> None:
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "run_id": self.run_id,
            "agent": self.agent_name,
            "level": logging.getLevelName(level),
            "message": message,
            "data": data or {},
        }
        log_path = _logs_dir() / f"{self.run_id}.jsonl"
        line = json.dumps(entry, default=str) + "\n"
        with _lock_for(log_path):
            log_path.open("a", encoding="utf-8").write(line)


def get_logger(agent_name: str, run_id: str | None = None) -> AgentLogger:
    """Return a rich-configured logger for the given agent.

    Each agent name maps to a fixed terminal color and ``[AGENT_NAME]``
    prefix. When ``run_id`` is provided, structured JSONL entries are
    appended to ``vol-logs/{run_id}.jsonl``.

    Parameters
    ----------
    agent_name : str
        Snake-case agent identifier (e.g. ``"feed_scout"``).
    run_id : str, optional
        Pipeline run identifier for JSONL correlation.

    Returns
    -------
    AgentLogger
        Configured logger for the agent.
    """
    if agent_name not in _logger_cache:
        _logger_cache[agent_name] = AgentLogger(agent_name)
    logger = _logger_cache[agent_name]
    if run_id is not None:
        logger.set_run_id(run_id)
    return logger


def configure_log_level(level: int) -> None:
    """Set the minimum log level on all cached agent loggers.

    Parameters
    ----------
    level : int
        Standard ``logging`` level constant (e.g. ``logging.DEBUG``).
    """
    for logger in _logger_cache.values():
        logger.set_level(level)


def _run_log_timestamp(stem: str) -> datetime | None:
    """Parse a run log filename stem into a UTC timestamp.

    Parameters
    ----------
    stem : str
        Filename stem without extension (e.g. ``"20250606-143022"``).

    Returns
    -------
    datetime | None
        Parsed UTC timestamp, or ``None`` when the stem is not a run ID.
    """
    try:
        return datetime.strptime(stem, _RUN_LOG_ID_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _run_log_reference_time(path: Path) -> datetime | None:
    """Return the best-effort timestamp associated with a run log file.

    Uses the run ID embedded in the filename when parseable; otherwise falls
    back to the file modification time for non-standard names (e.g. tests).
    """
    parsed = _run_log_timestamp(path.stem)
    if parsed is not None:
        return parsed
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return None


def prune_old_run_logs(
    retention_days: int = RUN_LOG_RETENTION_DAYS,
    *,
    now: datetime | None = None,
) -> dict[str, int]:
    """Delete structured run logs older than the retention window.

    Called at pipeline start so ``vol-logs/`` keeps a rolling 90-day history.

    Parameters
    ----------
    retention_days : int, optional
        Number of days to retain run logs. Defaults to ``RUN_LOG_RETENTION_DAYS``.
    now : datetime, optional
        Reference time for age calculation. Defaults to current UTC time.

    Returns
    -------
    dict[str, int]
        Summary counts: ``deleted``, ``kept``, and ``skipped`` (unreadable files).
    """
    reference = now or datetime.now(timezone.utc)
    cutoff = reference - timedelta(days=retention_days)
    logs_dir = _logs_dir()

    deleted = 0
    kept = 0
    skipped = 0

    for path in logs_dir.glob("*.jsonl"):
        log_time = _run_log_reference_time(path)
        if log_time is None:
            skipped += 1
            continue
        if log_time < cutoff:
            try:
                path.unlink()
                deleted += 1
            except OSError:
                skipped += 1
        else:
            kept += 1

    return {
        "deleted": deleted,
        "kept": kept,
        "skipped": skipped,
        "retention_days": retention_days,
    }


def list_run_logs(limit: int = 5) -> list[dict[str, Any]]:
    """Return metadata for the most recent structured run log files.

    Parameters
    ----------
    limit : int, optional
        Maximum number of runs to return. Defaults to 5.

    Returns
    -------
    list[dict[str, Any]]
        Run summaries sorted newest-first with ``run_id``, ``started_at``,
        and ``entry_count``.
    """
    if limit < 1:
        return []

    log_files = sorted(
        _logs_dir().glob("*.jsonl"),
        key=lambda path: _run_log_reference_time(path) or datetime.min.replace(
            tzinfo=timezone.utc
        ),
        reverse=True,
    )

    runs: list[dict[str, Any]] = []
    for path in log_files[:limit]:
        reference = _run_log_reference_time(path)
        try:
            entry_count = sum(
                1
                for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            )
        except OSError:
            entry_count = 0
        runs.append(
            {
                "run_id": path.stem,
                "started_at": reference.isoformat() if reference else None,
                "entry_count": entry_count,
            }
        )
    return runs


def read_run_log_entries(
    run_id: str,
    *,
    agent: str | None = None,
    level: str | None = None,
) -> list[dict[str, Any]]:
    """Read structured JSONL entries for a pipeline run.

    Parameters
    ----------
    run_id : str
        Run identifier matching ``vol-logs/{run_id}.jsonl``.
    agent : str, optional
        When set, only entries for this agent are returned.
    level : str, optional
        When set, only entries at this level (e.g. ``"INFO"``) are returned.

    Returns
    -------
    list[dict[str, Any]]
        Parsed log entries in file order.
    """
    log_path = _logs_dir() / f"{run_id}.jsonl"
    if not log_path.is_file():
        return []

    entries: list[dict[str, Any]] = []
    for line in log_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if agent is not None and entry.get("agent") != agent:
            continue
        if level is not None and entry.get("level") != level:
            continue
        entries.append(entry)
    return entries
