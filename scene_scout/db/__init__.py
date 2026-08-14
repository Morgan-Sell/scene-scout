"""Database migration helpers for SceneScout."""

from __future__ import annotations

import json
import time
from pathlib import Path

from alembic.config import Config

from alembic import command
from scene_scout.config import PROJECT_ROOT

_ALEMBIC_INI = PROJECT_ROOT / "alembic.ini"
_DEBUG_LOG_PATH = Path(__file__).resolve().parents[2] / ".cursor" / "debug-2140b8.log"


def _debug_log(message: str, data: dict, hypothesis_id: str) -> None:
    # #region agent log
    try:
        payload = {
            "sessionId": "2140b8",
            "timestamp": int(time.time() * 1000),
            "location": "scene_scout/db/__init__.py",
            "message": message,
            "data": data,
            "hypothesisId": hypothesis_id,
        }
        _DEBUG_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _DEBUG_LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload) + "\n")
    except OSError:
        pass
    # #endregion


def alembic_config() -> Config:
    """Return Alembic configuration rooted at the project ``alembic.ini``."""
    alembic_dir = PROJECT_ROOT / "alembic"
    _debug_log(
        "alembic_config paths",
        {
            "project_root": str(PROJECT_ROOT),
            "alembic_ini": str(_ALEMBIC_INI),
            "alembic_ini_exists": _ALEMBIC_INI.is_file(),
            "alembic_dir_exists": alembic_dir.is_dir(),
            "cwd": str(Path.cwd()),
        },
        "H1",
    )
    return Config(str(_ALEMBIC_INI))


def run_migrations() -> None:
    """Apply all pending Alembic migrations for feedback and history databases."""
    _debug_log("run_migrations starting", {}, "H3")
    try:
        command.upgrade(alembic_config(), "head")
        _debug_log("run_migrations succeeded", {}, "H3")
    except Exception as exc:
        _debug_log(
            "run_migrations failed",
            {"error_type": type(exc).__name__, "error": str(exc)},
            "H3",
        )
        raise
