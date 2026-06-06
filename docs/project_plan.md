# SceneScout: Project Plan

## How to Use This Plan

Each subphase is a discrete, self-contained unit of work with:
- A single clear goal
- Specific files to create or modify
- A "done when" definition that can be verified without ambiguity

Subphases are sized to be handed directly to Cursor as a build instruction.
Complete and verify each subphase before starting the next.

**Cursor instruction format:**
> "Build subphase 2.3: [subphase title]. Relevant files: [files]. Done when: [criteria]."

---

## Phase 1 — Foundation
*Goal: Working RSS pipeline with full project skeleton.*

### 1.1 — Package Management and Project Structure ✓
**Files:** `pyproject.toml`, `uv.lock`, `.env.example`, `.gitignore`
**Done when:** `uv sync --all-extras` completes without error. Project installs cleanly
in a fresh virtual environment.

### 1.2 — Feed Configuration and Models ✓
**Files:** `config/global_feeds.yaml`, `scene_scout/models/feed.py`
**Done when:** `FeedConfig`, `RawFeedEntry`, `FeedHealthReport` validate correctly.
Feed config loads and returns only active feeds.

### 1.3 — Feed Scout Agent ✓
**Files:** `scene_scout/agents/feed_scout.py`, `scene_scout/config.py`
**Done when:** `feed_scout.run()` fetches all configured feeds concurrently. One feed
failure does not affect others. Every `RawFeedEntry` carries `run_id`. `validate_feed()`
returns a health report for a given URL.

### 1.4 — Feed Scout Tests ✓
**Files:** `tests/agents/test_feed_scout.py`
**Done when:** All tests pass with mocked HTTP. Covers: successful fetch, malformed feed,
unreachable feed, empty feed, multi-feed isolation, `run_id` propagation, `validate_feed()`.

---

## Phase 2 — Project Infrastructure
*Goal: All shared services, tooling, and skeleton code in place before any agent writes a real LLM call.*

### 2.1 — Logging Service
**Files:** `scene_scout/logging/logger.py`
**Done when:** `get_logger(agent_name)` returns a `rich`-configured logger. Each agent
name maps to its assigned color. Logs write to terminal with `[AGENT_NAME]` prefix in
color. Structured JSONL entries written to `vol-logs/{run_id}.jsonl`.

### 2.2 — Centralized LLM Service
**Files:** `scene_scout/services/llm.py`
**Done when:** `llm.complete(prompt, system, response_model, run_id, agent_name)` calls
LiteLLM, validates the response against `response_model`, handles retries with
exponential backoff, raises `LLMInfrastructureError` on API outage, raises
`LLMValidationError` on schema mismatch. Token usage logged per call.

### 2.3 — Prompt Loader Service
**Files:** `scene_scout/services/prompt_loader.py`
**Done when:** `render_prompt(name, **kwargs)` loads the named Jinja2 template from
`scene_scout/prompts/`, injects kwargs, and returns the rendered string. Raises
`FileNotFoundError` on missing template. Raises `jinja2.UndefinedError` on missing
variable. Unit tested for both failure cases.

### 2.4 — Orchestrator Skeleton and PipelineState
**Files:** `scene_scout/orchestrator.py`
**Done when:** `Orchestrator.run(prompt)` generates `run_id`, creates a `PipelineState`
dataclass, calls each agent stub in sequence, and returns a `PipelineResult` with counts
at each stage. Agent stubs return empty lists. `PipelineState` serializes to/from JSON
for `vol-pipeline-state`.

### 2.5 — CLI and UAT Skeleton
**Files:** `scene_scout/cli.py`
**Done when:** `uv run python -m scene_scout.cli uat --prompt "test"` runs without error.
Creates `output/uat_{run_id}/` directory. Writes empty `summary.json`. Logs appear in
correct agent colors. `--dry-run` and `--verbose` flags are recognized.

### 2.6 — Docker and Compose
**Files:** `docker/pipeline/Dockerfile`, `docker/web/Dockerfile`, `docker-compose.yml`
**Done when:** `docker-compose up` starts both containers without error. Pipeline
container runs the orchestrator stub. Web container starts a placeholder Gradio page.
Both containers resolve shared volume mounts.

### 2.7 — Batch Strategy Service
**Files:** `scene_scout/services/batch.py`
**Done when:** `get_batch_strategy(model)` returns `AnthropicBatchStrategy` for Claude
models and `ConcurrentBatchStrategy` for all others. Both implement the `BatchStrategy`
protocol. `ConcurrentBatchStrategy` is unit-testable with mocked LiteLLM calls.

### 2.8 — Cache Service
**Files:** `scene_scout/services/cache.py`
**Done when:** `CacheService` provides `get` and `set` for performer, neighborhood, and
vibe caches. TTL enforced on `get` — expired entries return `None`. Hit/miss counts are
logged. SQLite schema initialized on first use.

### 2.9 — Mermaid Diagrams
**Files:** `docs/diagrams/diagrams.md`, `docs/diagrams/system_architecture.mmd`,
`docs/diagrams/data_flow.mmd`
**Done when:** Both diagrams render correctly in Cursor via `diagrams.md` preview.

---

## Phase 3 — Event Extraction
*Goal: Convert raw feed entries into validated, structured event candidates.*

### 3.1 — EventCandidate Model
**Files:** `scene_scout/models/event.py`
**Done when:** `EventCandidate` Pydantic model validates correctly. All optional fields
accept `None`. `is_event` and `extraction_confidence` are required. Unit tests cover
valid and invalid input.

### 3.2 — Extraction Prompt
**Files:** `scene_scout/prompts/event_extraction.txt`
**Done when:** Prompt is a valid Jinja2 template. Renders correctly with a sample
`RawFeedEntry`. Returns schema-valid JSON when tested manually against the LLM.

### 3.3 — Event Extraction Agent
**Files:** `scene_scout/agents/event_extraction.py`
**Done when:** `extraction.run(entries, run_id)` calls `llm.complete()` per entry,
validates output against `EventCandidate`, discards entries where `is_event=False`,
logs discards with reason, and returns only valid event candidates. On `LLMValidationError`
per entry: log and skip. On `LLMInfrastructureError`: re-raise to orchestrator.

### 3.4 — Extraction Tests
**Files:** `tests/agents/test_event_extraction.py`,
`tests/fixtures/golden/event_extraction/`
**Done when:** Unit tests cover: valid extraction, `is_event=False` discard, schema
validation failure (record skipped), infrastructure error (re-raised). Golden file
fixtures stored for 5 representative RSS entry types.

---

## Phase 4 — Normalization, Deduplication, and Description Quality
*Goal: Clean, deduplicated, quality-scored NormalizedEvent records ready for enrichment.*

### 4.1 — NormalizedEvent Model
**Files:** `scene_scout/models/event.py` (extend)
**Done when:** `NormalizedEvent` includes all fields from architecture spec including
`description_quality_score`, `low_information`, and stable `id` (SHA-256 hash).

### 4.2 — Event Normalization Agent
**Files:** `scene_scout/agents/event_normalization.py`
**Done when:** `normalization.run(candidates, run_id)` parses dates via `dateutil`,
normalizes venue names, standardizes categories, validates URLs, generates stable `id`,
discards events outside the coming 7 days. Unparseable dates → log + discard. Unit
tested with edge-case date strings.

### 4.3 — Deduplication Agent
**Files:** `scene_scout/agents/deduplication.py`
**Done when:** Exact ID match collapses identical events. `rapidfuzz` fuzzy match
(threshold 0.85 on title + same date + venue) merges near-duplicates, keeping the
record from the highest `source_quality_score` feed. All merges logged. Unit tested
with labeled duplicate pairs.

### 4.4 — Description Quality Agent
**Files:** `scene_scout/agents/description_quality.py`
**Done when:** Deterministic rubric scores all 7 signals. `description_quality_score`
and `low_information` populated on every record. `DESCRIPTION_QUALITY_THRESHOLD`
read from `config.py`. Unit tests cover every rubric boundary condition.

### 4.5 — Pre-Enrichment Filter
**Files:** `scene_scout/orchestrator.py` (extend)
**Done when:** Filter applied after Description Quality. Discards: `low_information=True`,
outside coming week, in 2-week exclude window. Discard counts logged by reason and
included in UAT summary.

---

## Phase 5 — Enrichment Pipeline
*Goal: Enrich filtered events with performer intelligence, vibe tags, and hyper-local neighborhood context via the batch strategy.*

### 5.1 — EnrichedEvent Model
**Files:** `scene_scout/models/enrichment.py`
**Done when:** `EnrichedEvent`, `PerformerInfo` validate correctly. All enrichment
fields default to empty/null. Inherits cleanly from `NormalizedEvent`.

### 5.2 — Geocoding Service
**Files:** `scene_scout/services/geocoding.py`
**Done when:** `geocode_venue(venue, city)` queries Nominatim, returns `(lat, lon)` or
`None`. `get_nearby_pois(lat, lon, radius_m=1000)` returns list of POI dicts. Results
cached in `vol-cache` with 90-day TTL. Rate limit enforced (1 req/sec). Unit tested
with mocked Nominatim responses.

### 5.3 — Talent Scout Prompt and Agent
**Files:** `scene_scout/prompts/talent_scout.txt`,
`scene_scout/agents/talent_scout.py`
**Done when:** Agent identifies named performers in event descriptions. Checks performer
cache first. Submits uncached events to batch strategy. Applies results to
`EnrichedEvent.performers`. Performers with `confidence < 0.7` stored in cache but
`one_line_summary` set to `None`. On validation error per event: empty performers list.

### 5.4 — Vibe Classifier Prompt and Agent
**Files:** `scene_scout/prompts/vibe_classifier.txt`,
`scene_scout/agents/vibe_classifier.py`
**Done when:** Agent assigns 2–5 vibe tags per event from the controlled vocabulary.
Checks vibe cache first. Submits uncached events to batch strategy. Tags outside
controlled vocabulary rejected — record logged and `vibe_tags` set to `[]`. Tag
distribution logged per run.

### 5.5 — Neighborhood Scout Prompt and Agent
**Files:** `scene_scout/prompts/neighborhood_scout.txt`,
`scene_scout/agents/neighborhood_scout.py`
**Done when:** Agent geocodes each venue (Mode A) or falls back to Mode B. POI list
passed to LLM as structured context — LLM does not recall businesses. Checks
neighborhood cache first. On `neighborhood_confidence < 0.5`: context set to `None`.
On geocoding failure: Mode B fallback, logged as warning.

### 5.6 — Batch Orchestration in Pipeline
**Files:** `scene_scout/orchestrator.py` (extend)
**Done when:** Phase 1 submits a single batch job covering all three enrichment agents
for all filtered events. `PipelineState` written to `vol-pipeline-state` with `batch_id`.
Orchestrator polls every 5 minutes via `asyncio.sleep()`. Phase 2 reads `PipelineState`
and applies batch results. `vol-pipeline-state` cleared on successful completion.

### 5.7 — Enrichment Tests
**Files:** `tests/agents/test_talent_scout.py`, `tests/agents/test_vibe_classifier.py`,
`tests/agents/test_neighborhood_scout.py`, `tests/services/test_geocoding.py`,
`tests/fixtures/golden/enrichment/`
**Done when:** Unit tests cover: cache hit (no LLM call), cache miss (LLM called),
validation error (graceful fallback), vocabulary enforcement (vibe). Golden fixtures
for 5 event types per enrichment agent.

---

## Phase 6 — User Preference and Ranking
*Goal: Parse user profile from onboarding, score enriched events, produce ranked list with grounded explanations.*

### 6.1 — UserProfile and CuratorConfig Models
**Files:** `scene_scout/models/user.py`, `scene_scout/curator_config.py`
**Done when:** `UserProfile` validates with all fields. `CuratorConfig` loads
`prompts/curator_voice.txt` and defaults `name` to `"Allegra"`.

### 6.2 — User Preference Parse Prompt and Agent
**Files:** `scene_scout/prompts/user_preference_parse.txt`,
`scene_scout/agents/user_preference.py`
**Done when:** `user_preference.parse_cold_start(name, email, prompt, run_id)` calls
`llm.complete()`, validates against `UserProfile`, writes to `vol-profiles/profile.json`.
`load_profile()` reads and returns current profile. Raises clearly if no profile exists.

### 6.3 — Chroma Service
**Files:** `scene_scout/services/chroma.py`
**Done when:** `embed(text)` returns a float vector via `sentence-transformers`.
`similarity_score(event, collection)` returns cosine similarity; returns `0.0` if
collection is empty (cold start). `add_liked_event(event)` adds embedding to collection.
Unit tested with mocked embeddings.

### 6.4 — Ranking Explanation Prompt
**Files:** `scene_scout/prompts/ranking_explanation.txt`
**Done when:** Prompt is a valid Jinja2 template. Receives score breakdown and event
fields. Returns a specific explanation — not generic. Tested manually.

### 6.5 — Ranking Agent
**Files:** `scene_scout/agents/ranking.py`
**Done when:** `ranking.run(events, profile, run_id)` computes deterministic scores for
all 8 components. Score breakdown logged per event. `llm.complete()` generates
explanation grounded in breakdown. Wildcard slots assigned. On explanation
`LLMValidationError`: fallback explanation used; logged as warning. Score components
are named constants in `config.py`.

### 6.6 — Feedback Token Infrastructure
**Files:** `scene_scout/services/feedback.py`,
`scene_scout/services/history.py`
**Done when:** `generate_feedback_token()` returns a UUID. `FeedbackEvent` schema
validated. `history.write_recommendations()` and `history.get_recent()` work correctly
against SQLite. `feedback.log_signal()` writes to `vol-feedback`.

### 6.7 — Ranking Tests
**Files:** `tests/agents/test_ranking.py`,
`tests/fixtures/golden/ranking/`
**Done when:** Unit tests cover: deterministic scoring with fixed inputs, score component
isolation, wildcard slot assignment, explanation fallback. Golden fixtures for 3 user
profiles × 5 event types.

---

## Phase 7 — Curation, Email, and Full UAT
*Goal: Final top 10 selected, email composed, real email sent in UAT. End-to-end verified.*

### 7.1 — Sell-Out Risk Agent
**Files:** `scene_scout/agents/sellout_risk.py`
**Done when:** Heuristic classifier assigns `"low"`, `"medium"`, or `"high"` to every
`RankedEvent`. Signals: venue size category, price, proximity to date, description
language, `top_performer_affinity`. Distribution logged. Unit tested.

### 7.2 — Recommendation History Service
**Files:** `scene_scout/services/history.py` (complete)
**Done when:** `get_recent(days)` returns recent recommendation entries. Recency
penalties applied correctly (4-week soft, 2-week hard). `update_feedback()` populates
`feedback_signal` on the relevant history entry.

### 7.3 — Curator Prompt and Agent
**Files:** `scene_scout/prompts/curator_voice.txt`,
`scene_scout/agents/recommendation_curator.py`
**Done when:** `curator.run(ranked_events, profile, run_id)` applies all diversity rules,
checks recommendation history, assigns wildcard slots, generates `CuratedRecommendation`
list. Sub-10 flagged with `below_minimum=True` for Email Composer. Allegra's voice brief
loaded from `curator_voice.txt`.

### 7.4 — Email Composer Prompt and Agent
**Files:** `scene_scout/prompts/email_composer.txt`,
`scene_scout/agents/email_composer.py`
**Done when:** `email_composer.run(recs, profile, run_id)` generates HTML email via
`llm.complete()` using only provided event data. All tracking links constructed
deterministically. Resend API called with `USER_EMAIL` from Modal Secret. UAT subject
prefixed `[UAT {run_id}]`. `--dry-run` writes preview file, skips send. Resend failure
raises `LLMInfrastructureError`.

### 7.5 — Gradio UI: Onboarding and Profile
**Files:** `scene_scout/gradio_app.py` (new)
**Done when:** Gradio app with built-in auth renders. Onboarding tab accepts name,
email, and cold-start prompt. Submits to User Preference Agent. Profile viewer tab
displays current `UserProfile` fields. Password loaded from `GRADIO_PASSWORD` env var.

### 7.6 — Full End-to-End UAT
**Files:** `scene_scout/cli.py` (complete), `scene_scout/orchestrator.py` (complete)
**Done when:** `uv run python -m scene_scout.cli uat --prompt "..."` runs the full
pipeline, produces a real email at `USER_EMAIL` with subject `[UAT {run_id}]`, writes
`output/uat_{run_id}/email_preview.html`, and prints the pipeline summary table.
Email opens correctly in an inbox. All tracking links are valid URLs. Allegra's voice
is present in the intro.

---

## Phase 8 — Feedback Loop
*Goal: Behavioral signals captured, profile updates applied, learning loop active.*

### 8.1 — Modal Tracking Endpoint
**Files:** `scene_scout/web/endpoints.py`
**Done when:** `GET /track?token=X&signal=click&redirect=URL` logs `FeedbackEvent`
to `vol-feedback` and redirects immediately. `GET /feedback?token=X&signal=negative`
logs signal and returns a simple confirmation page. Unknown tokens handled gracefully.

### 8.2 — Profile Update Logic
**Files:** `scene_scout/agents/user_preference.py` (extend)
**Done when:** `apply_feedback_signals(profile, signals)` applies decay-weighted delta
updates to `category_weights` and `vibe_preferences`. Decay factor `e^(-λt)`, half-life
30 days. Updated profile written to `vol-profiles`. `add_liked_event()` called on
Chroma service for click signals.

### 8.3 — Feedback Loop Integration Test
**Files:** `tests/integration/test_feedback_loop.py`
**Done when:** Integration test simulates: run pipeline → receive click signal → apply
profile update → verify `category_weights` changed in expected direction → verify Chroma
collection updated. Runs against real SQLite and Chroma (mocked LLM).

---

## Phase 9 — Sell-Out Risk and Evaluation
*Goal: Urgency signals in email, system quality monitored.*

### 9.1 — Sell-Out Risk Urgency Notes
**Files:** `scene_scout/agents/sellout_risk.py` (extend)
**Done when:** `sellout_urgency_note` populated for all `"high"` risk events. Note
surfaces in email via Email Composer. Future ML model design documented in
`docs/sellout_risk_ml.md`.

### 9.2 — Evaluation Prompt and Agent
**Files:** `scene_scout/prompts/evaluation.txt`,
`scene_scout/agents/evaluation.py`
**Done when:** `evaluation.run(recs, profile, run_id)` produces a quality report with
`overall_quality` score, flagged recommendations, and list-level issues. Report written
to `output/{run_id}/evaluation_report.json`. Runs after Email Composer in the pipeline.

### 9.3 — Evaluation Tests
**Files:** `tests/agents/test_evaluation.py`
**Done when:** Unit tests cover: generic explanation detection, missing field flagging,
category diversity check. Mocked LLM responses.

---

## Phase 10 — Observability and Dev Section
*Goal: Pipeline is fully observable. Dev Section operational in Gradio.*

### 10.1 — Structured Run Logging
**Files:** `scene_scout/logging/logger.py` (extend),
`scene_scout/orchestrator.py` (extend)
**Done when:** Every agent writes structured log entries to `vol-logs/{run_id}.jsonl`
with fields: `run_id`, `agent`, `level`, `message`, `data` (counts, cache stats,
score distributions). 90-day retention enforced at pipeline start.

### 10.2 — Gradio Dev Section
**Files:** `scene_scout/gradio_app.py` (extend)
**Done when:** Dev Section tab displays: last 5 run logs (filterable by agent and
level), feed health dashboard (last fetch per feed), dry-run trigger with prompt input
and email preview, recommendation history browser, cache hit rate summary.

### 10.3 — LLM-as-Judge Reranking Design
**Files:** `docs/llm_reranking.md`
**Done when:** Design document describes the v2 reranking approach: deterministic
scoring → top 20 candidates → LLM editorial layer → final top 10. Includes: prompt
design, schema, evaluation approach, and comparison methodology against v1.

### 10.4 — Regression Tests for Ranking
**Files:** `tests/regression/test_ranking_stability.py`
**Done when:** Regression tests assert that the same `EnrichedEvent` list and
`UserProfile` produce identical `score_breakdown` values across runs. Tests catch any
accidental introduction of stochasticity into the scoring path.

### 10.5 — Prompt Improvement Workflow
**Files:** `docs/prompt_improvement.md`
**Done when:** Document describes: how to regenerate golden file fixtures, how to
compare prompt versions using the Evaluation Agent, how to run prompt regression tests,
and how to decide when a prompt change is an improvement.

---

## Open Items

| Item | Status | Owner | Phase |
|---|---|---|---|
| Curator name | ✓ Allegra | — | — |
| Single-user architecture | ✓ Confirmed | — | — |
| Batch strategy (long-running Modal function) | ✓ Confirmed | — | 5 |
| Agent communication (direct returns + PipelineState) | ✓ Confirmed | — | 2 |
| Failure handling (graceful at record, fast at infra) | ✓ Confirmed | — | 2 |
| LLM service (`services/llm.py`) | ✓ Confirmed | — | 2 |
| Prompt templating (Jinja2) | ✓ Confirmed | — | 2 |
| Email source of truth (Modal Secret `USER_EMAIL`) | ✓ Confirmed | — | 7 |
| Log retention (90 days) | ✓ Confirmed | — | 10 |
| Test strategy (mock unit / golden regression) | ✓ Confirmed | — | 3 |
| Gradio auth (built-in) | ✓ Confirmed | — | 7 |
| Pre-enrichment filter threshold | Provisional 0.3 — tune after Phase 4 data | — | 5 |
| Feedback endpoint rate limiting | Known gap; acceptable for single-user | — | Post-launch |
| LLM model for Evaluation Agent | Open — smaller model TBD | — | 9 |
| Sell-out risk ML model design | Defer to Phase 9 | — | 9 |

---

## Cursor Build Instructions

Each subphase can be given to Cursor verbatim. Recommended format:

```
Build subphase {N.M}: {title}.

Files to create or modify: {file list}

Architecture reference: docs/architecture.md — {relevant section}

Done when: {done-when criteria}

Standards:
- NumPy-style docstrings on all public functions and classes
- Type hints on all function signatures
- Pydantic v2 for all schemas
- LiteLLM via services/llm.py — no direct provider imports
- Prompts via render_prompt() — no inline prompt strings
- rich logger via get_logger(agent_name) — no print statements
- Degrade gracefully at record level; raise on infrastructure failure
```

Complete and verify each subphase before starting the next.
Do not combine subphases in a single Cursor instruction.
