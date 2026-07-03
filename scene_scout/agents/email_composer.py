"""
Email Composer Agent

Responsibility
--------------
Generate a personalized HTML weekly email from curated recommendations via LLM
copy generation, assemble deterministic tracking links, and send through Resend.

Design
------
Inputs  : list[CuratedRecommendation], UserProfile, run_id: str
Outputs : EmailComposerResult with rendered HTML and optional send confirmation
"""

from __future__ import annotations

import html
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

from scene_scout.config import TRACKING_BASE_URL, is_dry_run
from scene_scout.curator_config import CuratorConfig, load_curator_config
from scene_scout.email_composer_config import (
    EMAIL_SUBJECT_SUFFIX,
    UAT_SUBJECT_PREFIX,
    email_preview_path,
)
from scene_scout.logging import get_logger
from scene_scout.models.curated import CuratedRecommendation
from scene_scout.models.email import EmailComposerLLMOutput, EmailComposerResult
from scene_scout.services.llm import (
    LLMValidationError,
    complete,
)
from scene_scout.services.prompt_loader import render_prompt
from scene_scout.services.resend import send_html_email

if TYPE_CHECKING:
    from scene_scout.models.user import UserProfile

_SYSTEM_PROMPT = (
    "You are Allegra, SceneScout's recommendation curator. "
    "Return only valid JSON matching the requested schema."
)


def build_subject(run_id: str, *, user_name: str) -> str:
    """Return the UAT-prefixed email subject line."""
    prefix = UAT_SUBJECT_PREFIX.format(run_id=run_id)
    return f"{prefix} {EMAIL_SUBJECT_SUFFIX} — {user_name}"


def build_track_url(
    token: str,
    redirect_url: str,
    *,
    base_url: str = TRACKING_BASE_URL,
) -> str:
    """Return a click-tracking URL that redirects to the event page."""
    query = urlencode(
        {
            "token": token,
            "signal": "click",
            "redirect": redirect_url,
        }
    )
    return f"{base_url.rstrip('/')}/track?{query}"


def build_feedback_url(
    token: str,
    *,
    base_url: str = TRACKING_BASE_URL,
) -> str:
    """Return a negative-feedback URL for a recommendation."""
    query = urlencode({"token": token, "signal": "negative"})
    return f"{base_url.rstrip('/')}/feedback?{query}"


def _profile_summary(profile: UserProfile) -> str:
    parts: list[str] = []
    if profile.stated_interests:
        parts.append(f"Interests: {', '.join(profile.stated_interests)}")
    if profile.vibe_preferences:
        parts.append(f"Vibe preferences: {', '.join(profile.vibe_preferences)}")
    if profile.preferred_neighborhoods:
        parts.append(
            "Preferred neighborhoods: " + ", ".join(profile.preferred_neighborhoods)
        )
    if profile.stated_dislikes:
        parts.append(f"Dislikes: {', '.join(profile.stated_dislikes)}")
    return "; ".join(parts) if parts else "General event discovery"


def _week_of_label(
    recommendations: list[CuratedRecommendation],
    *,
    now: datetime,
) -> str:
    if recommendations:
        first_date = recommendations[0].event.start_datetime.astimezone(timezone.utc)
        return first_date.strftime("%B %d, %Y")
    return now.astimezone(timezone.utc).strftime("%B %d, %Y")


def _format_price(*, is_free: bool, price_cents: int | None) -> str | None:
    if is_free:
        return "Free"
    if price_cents is None:
        return None
    return f"${price_cents / 100:.2f}"


def _format_event_datetime(
    start_datetime: datetime,
    end_datetime: datetime | None = None,
) -> str:
    localized_start = start_datetime.astimezone(timezone.utc)
    start_label = localized_start.strftime("%a, %b %d · %I:%M %p UTC").replace(
        " 0", " "
    )
    if end_datetime is None:
        return start_label

    localized_end = end_datetime.astimezone(timezone.utc)
    if localized_end.date() == localized_start.date():
        return start_label

    end_label = localized_end.strftime("%a, %b %d · %I:%M %p UTC").replace(" 0", " ")
    return f"{start_label} – {end_label}"


def build_event_blocks(recommendations: list[CuratedRecommendation]) -> str:
    """Format recommendation data for the LLM prompt."""
    blocks: list[str] = []
    for recommendation in recommendations:
        event = recommendation.event
        when_label = _format_event_datetime(
            event.start_datetime,
            event.end_datetime,
        )
        lines = [
            f"Event {recommendation.rank}:",
            f"  Title: {event.title}",
            f"  When: {when_label}",
            f"  Venue: {event.venue}",
            f"  City: {event.city}",
        ]
        price = _format_price(is_free=event.is_free, price_cents=event.price_cents)
        if price:
            lines.append(f"  Price: {price}")
        if event.categories:
            lines.append(f"  Categories: {', '.join(event.categories)}")
        if event.vibe_tags:
            lines.append(f"  Vibe tags: {', '.join(event.vibe_tags)}")
        if recommendation.is_wildcard:
            lines.append("  Wildcard slot: yes")
        lines.append(f"  Why picked: {recommendation.explanation}")
        if recommendation.neighborhood_context:
            lines.append(
                f"  Neighborhood context: {recommendation.neighborhood_context}"
            )
        if recommendation.sellout_urgency_note:
            lines.append(f"  Urgency: {recommendation.sellout_urgency_note}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def render_html_email(
    recommendations: list[CuratedRecommendation],
    *,
    intro_paragraph: str,
    event_descriptions: list[str],
    curator_name: str,
    user_name: str,
    tracking_base_url: str = TRACKING_BASE_URL,
) -> str:
    """Assemble the final HTML email with deterministic tracking links."""
    if len(event_descriptions) != len(recommendations):
        raise LLMValidationError(
            "LLM returned "
            f"{len(event_descriptions)} event descriptions for "
            f"{len(recommendations)} recommendations"
        )

    sections: list[str] = [
        "<!DOCTYPE html>",
        "<html>",
        "<head>",
        '<meta charset="utf-8">',
        "<title>SceneScout</title>",
        "</head>",
        '<body style="font-family: Georgia, serif; color: #1a1a1a; '
        'line-height: 1.6; max-width: 640px; margin: 0 auto; padding: 24px;">',
        f"<p>{html.escape(intro_paragraph)}</p>",
    ]

    for recommendation, description in zip(
        recommendations,
        event_descriptions,
        strict=True,
    ):
        event = recommendation.event
        track_url = build_track_url(
            recommendation.feedback_token,
            event.url,
            base_url=tracking_base_url,
        )
        feedback_url = build_feedback_url(
            recommendation.feedback_token,
            base_url=tracking_base_url,
        )
        price = _format_price(is_free=event.is_free, price_cents=event.price_cents)
        when_label = _format_event_datetime(
            event.start_datetime,
            event.end_datetime,
        )

        sections.extend(
            [
                '<section style="margin: 32px 0; padding-top: 16px; '
                'border-top: 1px solid #e5e5e5;">',
                f"<h2>{recommendation.rank}. "
                f'<a href="{html.escape(track_url, quote=True)}">'
                f"{html.escape(event.title)}</a></h2>",
                (
                    f"<p><strong>{html.escape(when_label)}</strong> · "
                    f"{html.escape(event.venue)} · "
                    f"{html.escape(event.city)}</p>"
                ),
            ]
        )
        if price:
            sections.append(f"<p>{html.escape(price)}</p>")
        sections.append(f"<p>{html.escape(description)}</p>")
        sections.append(f"<p><em>{html.escape(recommendation.explanation)}</em></p>")
        if recommendation.neighborhood_context:
            sections.append(
                f"<p>{html.escape(recommendation.neighborhood_context)}</p>"
            )
        if recommendation.sellout_urgency_note:
            sections.append(
                f"<p><strong>{html.escape(recommendation.sellout_urgency_note)}</strong></p>"
            )
        sections.append(
            f'<p><a href="{html.escape(track_url, quote=True)}">View event</a> · '
            f'<a href="{html.escape(feedback_url, quote=True)}">Not for me</a></p>'
        )
        if recommendation.is_wildcard:
            sections.append(
                f"<p><small>{html.escape(curator_name)}'s wildcard pick</small></p>"
            )
        sections.append("</section>")

    sections.extend(
        [
            f"<p>— {html.escape(curator_name)}</p>",
            "</body>",
            "</html>",
        ]
    )
    return "\n".join(sections)


async def _generate_copy(
    recommendations: list[CuratedRecommendation],
    profile: UserProfile,
    *,
    curator_name: str,
    below_minimum: bool,
    run_id: str,
    now: datetime,
) -> EmailComposerLLMOutput:
    """Generate intro and event descriptions via LLM."""
    return await complete(
        prompt=render_prompt(
            "email_composer",
            curator_name=curator_name,
            user_name=profile.name,
            profile_summary=_profile_summary(profile),
            recommendation_count=len(recommendations),
            week_of=_week_of_label(recommendations, now=now),
            event_blocks=build_event_blocks(recommendations),
            below_minimum=below_minimum,
        ),
        system=_SYSTEM_PROMPT,
        response_model=EmailComposerLLMOutput,
        run_id=run_id,
        agent_name="email_composer",
    )


async def run(
    recs: list[CuratedRecommendation],
    profile: UserProfile,
    run_id: str,
    *,
    below_minimum: bool = False,
    curator_config: CuratorConfig | None = None,
    now: datetime | None = None,
) -> EmailComposerResult:
    """Compose and optionally send the weekly recommendation email.

    Parameters
    ----------
    recs : list[CuratedRecommendation]
        Final curated recommendations from Allegra.
    profile : UserProfile
        Recipient profile for personalization and salutation.
    run_id : str
        Pipeline run identifier for logging and UAT subject prefix.
    below_minimum : bool
        When ``True``, the LLM includes Allegra's honest sub-10 note.
    curator_config : CuratorConfig, optional
        Allegra persona settings. Loaded from ``curator_voice.txt`` when omitted.
    now : datetime, optional
        Reference time for week-of labeling. Defaults to current UTC time.

    Returns
    -------
    EmailComposerResult
        Rendered HTML, subject, preview path, and send status.

    Raises
    ------
    LLMInfrastructureError
        On LLM provider outage or Resend delivery failure.
    LLMValidationError
        On invalid LLM response shape or description count mismatch.
    """
    logger = get_logger("email_composer", run_id=run_id)
    config = curator_config or load_curator_config()
    reference = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)

    if not recs:
        logger.warning("No recommendations to compose email for")
        subject = build_subject(run_id, user_name=profile.name)
        empty_html = (
            "<!DOCTYPE html><html><body>"
            f"<p>Hi {html.escape(profile.name)},</p>"
            f"<p>{html.escape(config.name)} did not find events worth recommending "
            "this week.</p>"
            "</body></html>"
        )
        preview = _write_preview(run_id, empty_html, logger)
        return EmailComposerResult(
            html=empty_html,
            subject=subject,
            preview_path=preview,
            sent=False,
        )

    llm_output = await _generate_copy(
        recs,
        profile,
        curator_name=config.name,
        below_minimum=below_minimum or len(recs) < 10,
        run_id=run_id,
        now=reference,
    )
    if len(llm_output.event_descriptions) != len(recs):
        raise LLMValidationError(
            "LLM returned "
            f"{len(llm_output.event_descriptions)} event descriptions for "
            f"{len(recs)} recommendations"
        )

    subject = build_subject(run_id, user_name=profile.name)
    html_body = render_html_email(
        recs,
        intro_paragraph=llm_output.intro_paragraph,
        event_descriptions=llm_output.event_descriptions,
        curator_name=config.name,
        user_name=profile.name,
    )
    preview = _write_preview(run_id, html_body, logger)

    if is_dry_run():
        logger.info(
            "Dry run — email preview written, send skipped",
            data={"preview_path": str(preview), "recommendation_count": len(recs)},
        )
        return EmailComposerResult(
            html=html_body,
            subject=subject,
            preview_path=preview,
            sent=False,
        )

    message_id = await send_html_email(subject=subject, html=html_body)
    logger.info(
        "Email sent via Resend",
        data={
            "resend_message_id": message_id,
            "recommendation_count": len(recs),
            "recipient": "USER_EMAIL",
        },
    )
    return EmailComposerResult(
        html=html_body,
        subject=subject,
        preview_path=preview,
        sent=True,
        resend_message_id=message_id,
    )


def _write_preview(run_id: str, html_body: str, logger: Any) -> Path:
    preview_path = email_preview_path(run_id)
    preview_path.parent.mkdir(parents=True, exist_ok=True)
    preview_path.write_text(html_body, encoding="utf-8")
    logger.info(
        "Email preview written",
        data={"preview_path": str(preview_path)},
    )
    return preview_path
