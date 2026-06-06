"""
Rich-configured logging for SceneScout agents.

Terminal output uses a color-coded ``[AGENT_NAME]`` prefix per agent.
Structured JSONL entries are written to ``vol-logs/{run_id}.jsonl`` for
production observability and the Gradio Dev Section log viewer.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.style import Style

_PROJECT_ROOT = Path(__file__).parent.parent.parent
_DEFAULT_LOGS_DIR = _PROJECT_ROOT / "vol-logs"

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
    "cache": ("CACHE", "dim white"),
    "llm": ("LLM SERVICE", "dim white"),
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


def _logs_dir() -> Path:
    """Return the directory for structured JSONL run logs.

    Returns
    -------
    Path
        Resolved log directory, created if it does not exist.
    """
    configured = os.getenv("VOL_LOGS_DIR")
    path = Path(configured) if configured else _DEFAULT_LOGS_DIR
    path.mkdir(parents=True, exist_ok=True)
    return path


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
        display_name, color = AGENT_COLORS.get(
            agent_name,
            (agent_name.upper().replace("_", " "), "white"),
        )
        self._display_name = display_name
        self._color = color
        self._console = Console(stderr=True, force_terminal=True)
        self._level = logging.INFO

    @property
    def display_name(self) -> str:
        """Human-readable agent label used in the terminal prefix."""
        return self._display_name

    @property
    def color(self) -> str:
        """Rich color name assigned to this agent."""
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

    def debug(self, message: str, *args: Any, data: dict[str, Any] | None = None) -> None:
        """Log a DEBUG-level message."""
        self._log(logging.DEBUG, message, *args, data=data)

    def info(self, message: str, *args: Any, data: dict[str, Any] | None = None) -> None:
        """Log an INFO-level message."""
        self._log(logging.INFO, message, *args, data=data)

    def warning(self, message: str, *args: Any, data: dict[str, Any] | None = None) -> None:
        """Log a WARNING-level message."""
        self._log(logging.WARNING, message, *args, data=data)

    def error(self, message: str, *args: Any, data: dict[str, Any] | None = None) -> None:
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
        agent_style = Style(color=self._color, bold=True)
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
