# SceneScout

A personalized, location-agnostic event discovery agent. It reads RSS feeds, extracts
and normalizes event data, enriches records with performer intelligence and hyper-local
context, ranks events against your taste profile, and delivers a curated weekly top 10
recommendation email.

SceneScout is a multi-agent application and a hands-on learning project in applied
agentic AI development, DevOps, and backend engineering.

## What it does

1. **Ingest** — Fetch configured RSS feeds with HTTP change detection (ETag / 304)
2. **Cache** — Skip re-extraction for entries seen in prior runs (`seen_entries`)
3. **Extract** — LLM converts raw feed entries into structured event candidates
4. **Normalize** — Parse dates, clean venues, validate URLs, assign stable IDs
5. **Deduplicate** — Collapse exact and fuzzy duplicates; merge cross-feed provenance
6. **Score quality** — Deterministic rubric flags sparse / low-information records
7. **Filter** — Drop low-quality and out-of-window events before enrichment
8. **Enrich** — Batch LLM calls for performers, vibe tags, and neighborhood context
9. **Rank** — Deterministic scoring against your profile; LLM explanations only
10. **Curate** — Select top 10 with diversity rules and one wildcard slot
11. **Deliver** — HTML email via Resend with tracking and feedback links
12. **Learn** — Feedback updates your profile and Chroma liked-event index over time

See [`docs/diagrams/diagrams.md`](docs/diagrams/diagrams.md) for the full data-flow
diagram (synced with [`docs/architecture.md`](docs/architecture.md)).

## Personalization

SceneScout uses a two-phase personalization model:

- **Cold start:** You write a prompt describing your interests and constraints. The User
  Preference Agent parses this into a structured profile and uses it immediately.
- **Warm personalization:** Over time, feedback — link clicks, "Not for me" signals, and
  (future) attendance — updates `category_weights` and `vibe_preferences` with decay.
  Chroma embeddings provide semantic similarity to events you liked.

Ranking signals include category fit, vibe fit, performer affinity, location, novelty,
source quality, multi-feed source coverage, and description quality.

## Agents

Each agent owns discrete inputs, outputs, and failure modes. Record-level failures
degrade gracefully; infrastructure failures fail fast.

| Agent | Responsibility | Status |
|---|---|---|
| Feed Scout | Fetch and parse RSS; ETag/304; feed health reports | Implemented |
| Event Extraction | LLM extraction → `EventCandidate`; discard non-events | Implemented |
| Event Normalization | Parse dates, clean venues, IDs, 7-day window filter | Implemented |
| Deduplication | Exact ID + fuzzy merge; union `source_feeds` / `source_count` | Implemented |
| Description Quality | Deterministic rubric → `description_quality_score`, `low_information` | Implemented |
| Talent Scout | Named performers; `performer_cache`; batch LLM | Planned (Phase 5) |
| Vibe Classifier | 2–5 tags from controlled vocabulary; `vibe_cache` | Planned (Phase 5) |
| Neighborhood Scout | Geocoding + POI context; `venue_cache`; batch LLM | Planned (Phase 5) |
| User Preference | Parse cold-start prompt; apply feedback deltas | Planned (Phase 7–8) |
| Ranking | Deterministic score + LLM explanation; Chroma similarity | Planned (Phase 6) |
| Sell-Out Risk | Urgency notes for high-risk events | Planned (Phase 9) |
| Recommendation Curator (Allegra) | Top 10 with diversity rules + wildcard | Planned (Phase 6) |
| Email Composer | HTML email via Resend; tracking links | Planned (Phase 7) |
| Evaluation | Post-send quality report | Planned (Phase 9) |

Full specs: [`docs/architecture.md`](docs/architecture.md). Milestone plan:
[`docs/project_plan.md`](docs/project_plan.md).

## Platform services

Shared infrastructure (not agents) that agents depend on:

| Service | Role |
|---|---|
| `services/llm.py` | Single LiteLLM entry point; retries; schema validation |
| `services/prompt_loader.py` | Jinja2 prompts in `scene_scout/prompts/` |
| `services/cache.py` | SQLite: `feed_etags`, `seen_entries`, performer/venue/vibe caches |
| `services/batch.py` | `BatchStrategy` — Anthropic batch vs concurrent async |
| `logging/logger.py` | Color-coded terminal logs + JSONL to `vol-logs/` |
| `orchestrator.py` | Sequences agents; `PipelineState` at enrichment batch boundary |
| `cli.py` | UAT mode: `uv run python -m scene_scout.cli uat --prompt "..."` |

Persistent volumes (local dev via env vars; production on Modal): `vol-cache`,
`vol-logs`, `vol-pipeline-state`, `vol-chroma`, `vol-history`, `vol-profiles`,
`vol-feedback`.

## Key features

- **Cross-feed deduplication** — Same event from multiple feeds merges with
  `source_count` as a weak ranking signal
- **seen_entries cache** — Known entries bypass extraction and normalization for 14 days
- **Explanation per recommendation** — Grounded in score breakdown, not generic prose
- **Feedback loop** — "Not for me" and click tracking update your profile over time
- **Recommendation memory** — History store avoids repeating recent picks
- **Hyper-local context** — Neighborhood Scout narrates POIs within ~1 km of the venue
- **Gradio UI** *(planned)* — Onboarding, profile review, feed health dev section

## Development

```bash
uv sync --all-extras
uv run pytest
uv run python -m scene_scout.cli uat --prompt "jazz and outdoor events" --dry-run
```

Copy `.env.example` to `.env` for local configuration. Use `--dry-run` to run the
pipeline without sending email. Full UAT with a real inbox is a manual release gate.

**CI:** GitHub Actions runs ruff, pytest with coverage (80% floor), and posts a coverage table on PRs (`.github/workflows/ci.yml`).

**Deploy:** Production target is Modal (scheduled pipeline + Gradio endpoint) — Phase 11,
not yet implemented. Local dev uses `docker-compose` for container parity.

## Status

**Early development — ingest through description quality is implemented; enrichment,
ranking, email, and feedback are stubbed in the orchestrator.**

| Phase | Scope | State |
|---|---|---|
| 1 | Feed Scout, models, tests | Done |
| 2 | LLM, cache, batch, logging, CI, diagrams | Done |
| 3 | Extraction agent, golden tests, `seen_entries` in orchestrator | Done |
| 4 | Normalization, deduplication, description quality, pre-enrichment filter | Done |
| 5 | Enrichment batch (Talent, Vibe, Neighborhood) | Planned |
| 6–7 | Ranking, curator, email, Gradio | Planned |
| 8–9 | Feedback loop, evaluation, sell-out risk | Planned |
| 11 | Modal deploy + CD | Planned |
