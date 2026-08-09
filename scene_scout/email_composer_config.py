"""Email Composer configuration for SceneScout."""

from __future__ import annotations

from pathlib import Path

from scene_scout.config import PROJECT_ROOT

UAT_SUBJECT_PREFIX = "[UAT {run_id}]"
EMAIL_SUBJECT_SUFFIX = "This week from Allegra"
EMAIL_PREVIEW_FILENAME = "email_preview.html"
EVALUATION_REPORT_FILENAME = "evaluation_report.json"


def uat_output_dir(run_id: str) -> Path:
    """Return the UAT output directory for a pipeline run."""
    return PROJECT_ROOT / "output" / f"uat_{run_id}"


def evaluation_report_path(run_id: str) -> Path:
    """Return the path where evaluation reports are written."""
    return uat_output_dir(run_id) / EVALUATION_REPORT_FILENAME


def email_preview_path(run_id: str) -> Path:
    """Return the path where UAT email previews are written."""
    return uat_output_dir(run_id) / EMAIL_PREVIEW_FILENAME
