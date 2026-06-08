# SceneScout

A personalized event discovery agent that scans local RSS feeds, ranks events against your taste profile, and delivers a curated weekly top 10 recommendation email.

SceneScout is designed as a multi-agent application and a hands-on learning project in applied agentic AI development.

## What it does

1. Reads configured RSS feeds from local event sources
2. Extracts and normalizes structured event data
3. Deduplicates events across sources
4. Ranks events against your personal taste profile
5. Curates a top 10 recommendation list with explanations
6. Sends a weekly email with feedback links
7. Learns from your feedback over time

## Personalization

SceneScout uses a two-phase personalization model:

- **Cold start:** You write a prompt describing your interests and constraints. The system parses this into a structured profile and uses it immediately.
- **Warm personalization:** Over time, your feedback — "Not for me" clicks and engagement signals — updates your profile. The system drifts toward what you actually respond to.

## Architecture

SceneScout is a multi-agent system. Each agent owns a discrete responsibility with defined inputs, outputs, and failure modes.

| Agent | Responsibility |
|---|---|
| Feed Scout | Read and validate RSS feeds |
| Event Extraction | Convert raw feed entries to structured event candidates |
| Event Normalization | Clean and standardize extracted events |
| Deduplication | Collapse duplicate events across sources |
| User Preference | Parse and maintain the user's taste profile |
| Ranking | Score events against the profile; generate explanations |
| Sell-Out Risk | Estimate ticket urgency |
| Recommendation Curator | Select the final top 10 with diversity controls |
| Email Composer | Write and send the weekly email |
| Evaluation | Review output quality and support improvement |

See [`docs/architecture.md`](docs/architecture.md) for full agent specs, schemas, data flow, and milestone plan.

## Key features

- **Explanation per recommendation** — every pick includes a reason grounded in your profile
- **Feedback button** — "Not for me" links in every email feed the learning loop
- **Recommendation memory** — the system tracks what it has already sent and avoids repetition

## Status

Early development. See [`docs/project_plan.md`](docs/project_plan.md) for the milestone
plan and [`docs/architecture.md`](docs/architecture.md) for full specs.

**Testing & delivery:** Unit tests run locally with `uv run pytest`. GitHub Actions CI
(Phase 2.10) will gate PRs. Full UAT with real email is a manual release check; use
`--dry-run` for day-to-day pipeline iteration. Production deploy targets Modal
(Phase 11) — not yet implemented.
