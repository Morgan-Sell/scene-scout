# SceneScout

A personalized, location-agnostic event discovery agent. It reads RSS feeds, extracts
and normalizes event data, enriches records with performer intelligence and hyper-local
context, ranks events against your taste profile, and delivers a curated weekly top 10
recommendation email.

SceneScout is a multi-agent application and a hands-on learning project in applied
agentic AI development, DevOps, and backend engineering.

**Current milestone:** Phases 1–5 are complete — ingest through enrichment runs end-to-end
in the orchestrator (with Feed Scout still stubbed for UAT). Ranking, email delivery,
and the feedback loop are next.

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
| Feed Scout | Fetch and parse RSS; ETag/304; feed health reports | Implemented *(orchestrator stub — wired in Phase 7)* |
| Event Extraction | LLM extraction → `EventCandidate`; discard non-events | Implemented |
| Event Normalization | Parse dates, clean venues, IDs, 7-day window filter | Implemented |
| Deduplication | Exact ID + fuzzy merge; union `source_feeds` / `source_count` | Implemented |
| Description Quality | Deterministic rubric → `description_quality_score`, `low_information` | Implemented |
| Talent Scout | Named performers; `performer_cache`; batch LLM | Implemented |
| Vibe Classifier | 2–5 tags from controlled vocabulary; `vibe_cache` | Implemented |
| Neighborhood Scout | Geocoding + POI context; `venue_cache`; batch LLM | Implemented |
| User Preference | Parse cold-start prompt; apply feedback deltas | Planned (Phase 6–8) |
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
| `services/geocoding.py` | Nominatim geocoding + Overpass POIs; rate-limited; `venue_cache` |
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
- **Enrichment batch orchestration** — Talent, Vibe, and Neighborhood scouts share one
  batch job; `PipelineState` persists across the poll boundary
- **Enrichment caches** — Performer (90-day), venue geo (90-day), vibe (14-day) TTLs
  reduce repeat LLM and geocoding calls
- **Hyper-local context** — Neighborhood Scout narrates POIs within ~1 km of the venue
  using geocoded coordinates (Mode A) or venue-name fallback (Mode B)
- **Explanation per recommendation** — Grounded in score breakdown, not generic prose
  *(planned — Phase 6)*
- **Feedback loop** — "Not for me" and click tracking update your profile over time
  *(planned — Phase 8)*
- **Recommendation memory** — History store avoids repeating recent picks *(planned — Phase 6)*
- **Web UI** — FastAPI onboarding and profile viewer; Dev Section planned (Phase 10)

## Tech stack

| Layer | Choice |
|---|---|
| Language | Python 3.12+ |
| Package manager | `uv` |
| Schemas | Pydantic v2 |
| LLM | LiteLLM (default: Claude via `LLM_MODEL`) |
| RSS / HTTP | `feedparser`, `httpx` (async) |
| Fuzzy dedup | `rapidfuzz` |
| Geocoding | Nominatim + Overpass (OpenStreetMap) |
| Vector store | Chroma + `sentence-transformers` *(Phase 6)* |
| Email | Resend *(Phase 7)* |
| UI | FastAPI + custom HTML/CSS/JS *(Phase 7.5)* |
| Deploy | Modal *(Phase 11)* |
| Local dev | Docker Compose (`pipeline` + `web` containers) |

## Project layout

```
scene-scout/
├── config/feeds.yaml          # RSS feed sources (city, quality score, active flag)
├── scene_scout/
│   ├── agents/                # One module per agent
│   ├── models/                # Pydantic schemas (feed, event, enrichment)
│   ├── prompts/               # Jinja2 LLM prompt templates
│   ├── services/              # LLM, cache, batch, geocoding, prompt loader
│   ├── orchestrator.py        # Pipeline sequencing and batch boundary
│   ├── cli.py                 # UAT entry point
│   └── web/                   # FastAPI onboarding and profile UI
├── tests/
│   ├── agents/                # Per-agent unit tests
│   ├── services/              # Cache, geocoding tests
│   └── fixtures/golden/       # Prompt regression fixtures (enrichment, extraction)
├── docs/                      # Architecture, project plan, diagrams
└── docker/                    # Pipeline and web container images
```

## Development

### Setup

```bash
uv sync --all-extras
cp .env.example .env   # fill in LLM_API_KEY for live runs
```

### Run tests

```bash
uv run pytest                              # full suite (~240+ tests)
uv run pytest tests/agents/ -v             # agent tests only
uv run ruff check scene_scout tests         # lint
uv run ruff format --check scene_scout tests
```

### Run the pipeline locally

```bash
# Dry run — no email; full pipeline with live feeds (requires LLM_API_KEY)
uv run python -m scene_scout.cli uat --prompt "jazz and outdoor events" --dry-run

# Verbose agent logs
uv run python -m scene_scout.cli uat --prompt "..." --dry-run --verbose
```

UAT writes per-run output to `output/uat_{run_id}/summary.json` with stage counts
(raw entries, cache hit rate, discards by reason, enriched events, etc.).

### Docker Compose

```bash
docker compose up --build
```

Starts the pipeline container (orchestrator stub) and web container (FastAPI UI on
port 7860). Shared named volumes mirror Modal persistent stores.

### Configuration

**Feeds** — Edit `config/feeds.yaml` to add or disable RSS sources. Each feed has an
`id`, `url`, `city`, `source_quality_score`, and `active` flag.

**Environment** — Copy `.env.example` to `.env`:

| Variable | Purpose |
|---|---|
| `DRY_RUN` | Skip email send when `true` |
| `LLM_MODEL` | LiteLLM model string (default: `claude-sonnet-4-6`) |
| `LLM_API_KEY` | Provider API key |
| `LLM_API_BASE` | Optional — Ollama or custom endpoint |
| `WEB_PASSWORD` | Web UI HTTP Basic auth password (optional; disabled when unset) |
| `VOL_*_DIR` | Override local volume paths (defaults under project root) |

Use `--dry-run` on the CLI to run the pipeline without sending email. Full UAT with a
real inbox is a manual release gate (Phase 7).

## Testing

- **CI:** GitHub Actions runs ruff, pytest with coverage (80% floor), and posts a
  coverage table on PRs (`.github/workflows/ci.yml`). No live API keys required.
- **Unit tests:** Mocked LLM and HTTP throughout; agents tested in isolation.
- **Golden fixtures:** Stored under `tests/fixtures/golden/` for extraction and
  enrichment prompt regression (run locally, not in CI).
- **Orchestrator integration:** End-to-end pipeline tests mock Feed Scout and verify
  stage counts, `seen_entries` caching, pre-enrichment discards, and enrichment batch
  application.

## Status

**Active development — ingest through enrichment is implemented and wired in the
orchestrator. Feed Scout, ranking, and email are wired for UAT; evaluation remains Phase 9.**

| Phase | Scope | State |
|---|---|---|
| 1 | Feed Scout, models, tests | Done |
| 2 | LLM, cache, batch, logging, CI, diagrams, Docker | Done |
| 3 | Extraction agent, golden tests, `seen_entries` in orchestrator | Done |
| 4 | Normalization, deduplication, description quality, pre-enrichment filter | Done |
| 5 | Enrichment batch (Talent, Vibe, Neighborhood), geocoding, golden tests | Done |
| 6 | User preference, ranking, Chroma, curator | Done |
| 7 | Email composer, web UI, full end-to-end UAT | Done |
| 8–9 | Feedback loop, evaluation | Planned |
| 11 | Modal deploy + CD | Planned |

### What's working today

- Full agent implementations for feed fetch, extraction, normalization, deduplication,
  description quality, and all three enrichment scouts
- Orchestrator runs extraction → filter → enrichment batch submit/poll/apply with
  `PipelineState` persistence at the batch boundary
- SQLite caches for ETags, seen entries, performers, venues, and vibes
- Geocoding service with Nominatim rate limiting and Overpass POI lookup
- 240+ passing tests with mocked LLM/HTTP and enrichment golden fixtures

### What's next

- Run live UAT: `uv run python -m scene_scout.cli uat --prompt "..." --dry-run` (needs `LLM_API_KEY`)
- Resend live-email gate (7.6 operator setup) for non-dry-run sends
- Modal deployment with scheduled runs and tracking endpoints (Phase 11)

---

*This README is a work in progress and tracks implementation status against
[`docs/project_plan.md`](docs/project_plan.md).*
