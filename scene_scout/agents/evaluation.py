"""
Evaluation Agent

Responsibility
--------------
Review curated recommendations for quality issues via LLM and persist a structured
report to the run output directory.

Design
------
Inputs  : list[CuratedRecommendation], UserProfile, run_id
Outputs : EvaluationReport written to ``output/uat_{run_id}/evaluation_report.json``
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from scene_scout.agents.email_composer import _profile_summary, build_event_blocks
from scene_scout.email_composer_config import evaluation_report_path
from scene_scout.logging import get_logger
from scene_scout.models.evaluation import EvaluationLLMOutput, EvaluationReport
from scene_scout.services.llm import LLMValidationError, complete
from scene_scout.services.prompt_loader import render_prompt

if TYPE_CHECKING:
    from scene_scout.models.curated import CuratedRecommendation
    from scene_scout.models.user import UserProfile

_SYSTEM_PROMPT = (
    "You are a recommendation quality evaluator for SceneScout. "
    "Return only valid JSON matching the requested schema."
)


def fallback_evaluation_output(
    recommendations: list[CuratedRecommendation],
) -> EvaluationLLMOutput:
    """Return deterministic evaluation output when LLM validation fails."""
    count = len(recommendations)
    return EvaluationLLMOutput(
        overall_quality=0.5 if count else 0.0,
        flagged_recommendations=[],
        list_level_issues=(
            ["Evaluation LLM response could not be validated."]
            if count
            else ["No recommendations to evaluate."]
        ),
        summary=(
            f"Fallback evaluation recorded for {count} recommendation"
            f"{'' if count == 1 else 's'}."
        ),
    )


def empty_evaluation_report(run_id: str) -> EvaluationReport:
    """Return a report when there are no recommendations to evaluate."""
    return EvaluationReport(
        run_id=run_id,
        recommendation_count=0,
        overall_quality=0.0,
        flagged_recommendations=[],
        list_level_issues=["No recommendations to evaluate."],
        summary="No curated recommendations were produced for this run.",
    )


def _write_report(report: EvaluationReport, *, logger: Any) -> EvaluationReport:
    path = evaluation_report_path(report.run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    written = report.model_copy(update={"report_path": path})
    path.write_text(
        json.dumps(written.model_dump(mode="json"), indent=2) + "\n",
        encoding="utf-8",
    )
    logger.info(
        "Evaluation report written",
        data={
            "report_path": str(path),
            "overall_quality": report.overall_quality,
            "flagged_count": len(report.flagged_recommendations),
            "list_level_issue_count": len(report.list_level_issues),
        },
    )
    return written


async def _evaluate_with_llm(
    recommendations: list[CuratedRecommendation],
    profile: UserProfile,
    *,
    run_id: str,
    logger: Any,
) -> EvaluationLLMOutput:
    try:
        return await complete(
            prompt=render_prompt(
                "evaluation",
                profile_summary=_profile_summary(profile),
                run_id=run_id,
                recommendation_blocks=build_event_blocks(recommendations),
                recommendation_count=len(recommendations),
            ),
            system=_SYSTEM_PROMPT,
            response_model=EvaluationLLMOutput,
            run_id=run_id,
            agent_name="evaluation",
        )
    except LLMValidationError as exc:
        logger.warning(
            "Evaluation LLM validation failed; using fallback report",
            data={"error": str(exc), "recommendation_count": len(recommendations)},
        )
        return fallback_evaluation_output(recommendations)


async def run(
    recs: list[CuratedRecommendation],
    profile: UserProfile,
    run_id: str,
) -> EvaluationReport:
    """Evaluate recommendation quality and write ``evaluation_report.json``.

    Parameters
    ----------
    recs : list[CuratedRecommendation]
        Final curated recommendations from Allegra.
    profile : UserProfile
        User taste profile used for alignment checks in the prompt.
    run_id : str
        Pipeline run identifier for logging and output paths.

    Returns
    -------
    EvaluationReport
        Structured quality report with path to the written JSON artifact.

    Raises
    ------
    LLMInfrastructureError
        On unrecoverable LLM provider failures.
    """
    logger = get_logger("evaluation", run_id=run_id)

    if not recs:
        logger.warning("No recommendations to evaluate")
        return _write_report(empty_evaluation_report(run_id), logger=logger)

    llm_output = await _evaluate_with_llm(recs, profile, run_id=run_id, logger=logger)
    report = EvaluationReport(
        run_id=run_id,
        recommendation_count=len(recs),
        **llm_output.model_dump(),
    )
    return _write_report(report, logger=logger)
