# SceneScout: Project Plan

## How to Use This Plan

Each subphase is a discrete, self-contained unit of work with:
- A single clear goal
- Specific files to create or modify
- A "done when" definition that can be verified without ambiguity

Subphases are sized to be handed directly to Cursor as a build instruction.
Complete and verify each subphase before starting the next.

**Cursor instruction format:**
> "Build subphase {N.M}: {title}. Files: {files}. Done when: {criteria}."

---

## Phase 1 — Foundation
*Goal: Working RSS pipeline with full project skeleton.*

### 1.1 — Package Management and Project Structure ✓
**Files:** `pyproject.toml`, `uv.lock`, `.env.example`, `.gitignore`
**Done when:** `uv sync --all-extras` completes without error. Project installs cleanly
in a fresh virtual environment.

### 1.2 — Feed Configuration and Models ✓
**Files:** `config/global_feeds.yaml`, `scene_scout/models/feed.py`
**Done when:** `FeedConfig` (including `cursor: Optional[str] = None` field),
`RawFeedEntry`, `FeedHealthReport`, and `FeedStatus` (including `UNCHANGED = "unchanged"`)
all validate correctly. Feed config loads and returns only active feeds.

> **Note:** Phase 1 shipped with a single `config/feeds.yaml` (operator-edited). The
> `global_feeds.yaml` / `user_feeds.yaml` split remains aspirational — see Phase 1B and
> Open Items.

### 1.3 — Feed Scout Agent ✓
**Files:** `scene_scout/agents/feed_scout.py`, `scene_scout/config.py`
**Done when:**
- `feed_scout.run()` fetches all configured feeds concurrently
- Sends `If-None-Match` and `If-Modified-Since` headers on every request
- On `304 Not Modified` response, returns `FeedHealthReport(status=FeedStatus.UNCHANGED)`
  with no entries — feed is skipped cleanly
- ETag and `Last-Modified` values are stored and retrieved from `feed_etags` table
  (storage implemented in Cache Service — stub the interface here, implement in 2.8)
- One feed failure does not affect others
- Every `RawFeedEntry` carries `run_id`
- `validate_feed()` returns a health report for a given URL

### 1.4 — Feed Scout Tests ✓
**Files:** `tests/agents/test_feed_scout.py`
**Done when:** All tests pass with mocked HTTP. Covers:
- Successful fetch
- `304 Not Modified` response → `UNCHANGED` status, no entries returned
- ETag stored after successful fetch; sent on subsequent request
- Malformed feed, unreachable feed, empty feed
- Multi-feed isolation (one failure doesn't stop others)
- `run_id` propagation to every entry
- `validate_feed()` success and failure

---

## Phase 1B — Multi-Source Ingestion
*Goal: Expand beyond RSS-only ingestion while keeping the `RawFeedEntry` downstream
contract unchanged.*

Phase 1 delivers a working RSS pipeline. UAT showed that RSS alone is a weak event
source: many feeds are editorial (news/lifestyle) rather than dated listings, and several
NYC targets (NYPL, BPL, Oh My Rockness, Dance/NYC) expose HTML calendars or APIs — not
RSS. Phase 1B adds pluggable source adapters without changing Event Extraction or
downstream agents.

**Branching:** One feature branch per subphase (e.g. `feat/1b-1-source-adapters`),
merged to `main` after Phase 7.7.

### 1B.1 — Source Type and Adapter Interface
**Files:** `scene_scout/models/feed.py`, `config/feeds.yaml`, `scene_scout/config.py`,
`scene_scout/agents/feed_scout.py` (refactor dispatch) or `scene_scout/agents/source_scout.py`
**Done when:**
- `FeedConfig` gains `source_type: Literal["rss", "ical", "api", "scrape"]` (default
  `"rss"` — backward compatible with existing config)
- Adapter protocol defined:
  `fetch(config, run_id, cache_hooks) -> tuple[list[RawFeedEntry], FeedHealthReport]`
- Feed Scout (or Source Scout) dispatches to the correct adapter per `source_type`
- RSS path behavior unchanged; existing Feed Scout tests pass
- `RawFeedEntry` contract unchanged — all adapters normalize to it
- Per-source health reporting works regardless of type
- `validate_feed()` extended to accept non-RSS URLs where applicable

### 1B.2 — iCal/ICS Adapter
**Files:** `scene_scout/agents/sources/ical.py` (or equivalent), `config/feeds.yaml`,
`tests/agents/test_ical_source.py`, `pyproject.toml` (add `icalendar` or equivalent)
**Done when:**
- iCal adapter fetches `.ics` URLs and maps `VEVENT` fields to `RawFeedEntry` (title,
  link, description, `published_raw` from DTSTART)
- Unit tests with fixture `.ics` files; health report on unreachable/malformed ICS
- UAT `feed_health` shows non-zero entries from iCal sources when configured

**UAT-D.11 footnote (Jul 2026):** Interim NYPL/BPL LibCal feeds were removed from
`config/feeds.yaml` — they dominated ingest with class-series noise and are not the
product direction. Re-add only when official public ICS endpoints exist. Active NYC
sources are independent listings (`brooklynvegan`, `theskint`, `brooklyn_rail`,
`harlem_one_stop`). See [UAT-D.11](260629_uat_debug_plan.md).

### 1B.3 — Event API Connector
**Files:** `scene_scout/agents/sources/event_api.py`, `scene_scout/config.py`,
`.env.example`, `config/feeds.yaml`, `tests/agents/test_event_api_source.py`
**Done when:**
- One event platform integrated (Eventbrite **or** Songkick — decide at build time)
- Uses existing `FeedConfig.cursor` for pagination / cursor state
- Geo filter aligned with feed `city` (New York for current config)
- API responses mapped to `RawFeedEntry`
- Tests mock HTTP; CI passes without live API keys
- Required env vars documented in `.env.example` (e.g. `EVENTBRITE_API_TOKEN`)

### 1B.4 — HTML Calendar Scraper (Deferred / Per-Site)
**Files:** `scene_scout/agents/sources/html_calendar.py`, `config/feeds.yaml`
(per-site selector config), `tests/agents/test_html_calendar_source.py`
**Done when:**
- Adapter supports site-specific scrape configuration (selectors or embedded JSON API
  discovery — prefer underlying XHR/JSON endpoints over brittle DOM scraping)
- At least one NYC HTML-only source implemented (e.g. Oh My Rockness show listings or
  Dance/NYC RSS builder output URL)
- Scraper failures produce `FeedHealthReport` without halting the pipeline
- **Defer** until 1B.1–1B.3 are complete; implement additional sites incrementally

> **Product direction (Jul 2026):** Indie/creative ingest is no longer the goal. See
> [`260705_product_redesign.md`](260705_product_redesign.md). Implementation continues in
> **Phase 1C** below.

---

## Phase 1C — Personalized Mainstream Discovery
*Goal: Align ingest, profile, and time windows with mainstream events and personalization —
without removing agents or Neighborhood Scout.*

**Rationale:** [`260705_product_redesign.md`](260705_product_redesign.md)

**Branching:** One feature branch per subphase (e.g. `feat/1c-1-profile-city-horizon`).

**Feed scoping model (1C.2–1C.3):** Each entry in `config/feeds.yaml` keeps a `city`
string (metro tag for local sources). National mainstream APIs (e.g. Ticketmaster,
Eventbrite when enabled) add `is_national: true` — they are **always** included in
ingest regardless of `UserProfile.home_city`. Metro feeds (`is_national: false`, the
default) are included only when `feed.city` matches `profile.home_city`. For
`is_national` API feeds, adapters must query using **`profile.home_city`** (via a metro
→ API geo mapping), not the static `feed.city` on the config row. Do not use
`city: "National"` as a filter sentinel; use the boolean.

### 1C.1 — UserProfile city and horizon (backend + onboarding UI)
**Files:**
`scene_scout/models/user.py`,
`scene_scout/prompts/user_preference_parse.txt`,
`scene_scout/agents/user_preference.py`,
`scene_scout/web/app.py`,
`scene_scout/web/static/index.html`,
`scene_scout/web/static/app.js`,
`scene_scout/cli.py` (UAT flags or documented fallback),
`tests/models/test_user.py`,
`tests/agents/test_user_preference.py`,
web UI tests or manual checklist (see Done when)

**Context:** Extends [Phase 7.5](#75--custom-web-ui-onboarding-and-profile) onboarding. City and
horizon are **explicit form fields**, not inferred only from the taste prompt. The LLM still
parses interests/dislikes from the prompt; `home_city` and `horizon_days` are passed through
from the UI (and stored on `UserProfile`).

**Backend — model and agent:**
- `UserProfile` gains `home_city: str` and `horizon_days: int` (validated range, e.g. 1–60)
- `UserProfileParseLLMOutput` unchanged for taste fields; city/horizon set on the profile
  from request args, not from LLM extraction
- `user_preference.parse_cold_start(..., home_city=..., horizon_days=...)` accepts both;
  writes them to `vol-profiles/profile.json`
- `POST /api/onboarding` body extends to
  `{name, email, home_city, horizon_days, prompt}`; server validates city non-empty and
  horizon in range before calling the agent

**Frontend — onboarding tab (`scene_scout/web/static/`):**
- **Home city** — text input (or select if a metro list is added later); required; placed
  above name/email; placeholder e.g. "New York"
- **Horizon (days out)** — number input; required; min/max aligned with model validation
  (e.g. 1–60); helper text e.g. "How far ahead to search for events"
- Form order matches [architecture](architecture.md): city → horizon → name → email → taste
- Client-side validation mirrors backend (city, horizon range, existing name/email/prompt rules)
- Submit sends all five fields in `POST /api/onboarding`
- Success summary and Profile tab display `home_city` and `horizon_days`

**UAT / CLI fallback (no web UI required for pipeline dev):**
- `uat` accepts `--city` and `--horizon-days` (or env vars documented in `.env.example`) when
  no persisted profile exists, so Tier B/C runs work without opening the browser

**Done when:**
- [ ] `UserProfile` validates `home_city` and `horizon_days`; unit tests cover bounds
- [ ] Web onboarding form shows city + horizon inputs; submit persists both fields
- [ ] Profile tab renders `home_city` and `horizon_days`
- [ ] Invalid horizon or empty city returns 422 with clear error on `POST /api/onboarding`
- [ ] UAT/CLI path documented for runs without a saved profile
- [ ] `docker-compose up` + manual smoke: complete onboarding in browser; reload Profile tab
  and confirm city/horizon visible

### 1C.2 — City-scoped feed loading (and national feeds)
**Files:**
`scene_scout/models/feed.py`,
`scene_scout/config.py`,
`scene_scout/orchestrator.py`,
`scene_scout/agents/feed_scout.py`,
`scene_scout/agents/sources/event_api.py` (and future national API adapters),
`scene_scout/cli.py` (`feed-probe`),
`config/feeds.yaml` (document `is_national` in header),
tests

**Context:** 1C.1 stores `home_city` on the profile. This subphase connects that field
to **which feeds run** and **what city national APIs query**. Two responsibilities:

1. **Filter (load time)** — include an active feed when `feed.is_national` **or**
   `feed.city == profile.home_city`.
2. **Fetch (adapter time)** — when `feed.is_national` and `source_type == api`, pass
   `profile.home_city` into platform geo/search params (extend `_CITY_SEARCH_PARAMS` or
   equivalent); metro-local RSS/scrape feeds continue to use `feed.city` for metadata only.

**Backend:**
- `FeedConfig` gains `is_national: bool = False`
- `load_feed_configs(home_city: str | None = None)` — when `home_city` is set, return
  active feeds matching the union rule above; when `None`, preserve today’s “all active
  feeds” behavior for backward-compatible operator scripts
- Orchestrator passes `profile.home_city` into `load_feed_configs()` before
  `feed_scout.run()`, and passes the same `home_city` into `feed_scout.run()` for adapters
- `feed-probe` supports optional `--city` using the same filter as the orchestrator

**Done when:**
- [ ] `FeedConfig.is_national` validated; documented in `feeds.yaml` header with examples
- [ ] `load_feed_configs(home_city=...)` returns metro feeds for that city **plus** all
  active `is_national` feeds
- [ ] Orchestrator wires `profile.home_city` through load + `feed_scout.run()`
- [ ] National API adapter(s) use `home_city` for geo/search params when `is_national`
  (Eventbrite path updated even if feed remains inactive until 1C.3)
- [ ] `feed-probe --city "New York"` exercises metro + national subset only
- [ ] Unit tests: mixed metro + national config — NYC user gets NYC + national, not LA-only;
  national feed included for any `home_city`

### 1C.3 — Mainstream metro feed catalog
**Files:** `config/feeds.yaml`, `.env.example`, docs cross-links
**Done when:**
- [ ] Feed catalog reflects redesign: structured mainstream sources per metro (e.g. DoNYC +
  metro-local listings); indie/editorial RSS and fragile scrapes removed or `active: false`
- [ ] National platform slots (Ticketmaster, Eventbrite when a working endpoint exists) use
  `is_national: true`; metro scrapes/RSS keep `is_national: false` with `city` set to
  the metro name (e.g. `New York`)
- [ ] Eventbrite search feed inactive until a working API endpoint is configured
- [ ] `feed-probe --city` for default metro returns non-zero entries from ≥2 independent sources
- [ ] Notes in config header point to `260705_product_redesign.md` and the `is_national` rule

**Default metro smoke:** `uv run python -m scene_scout.cli feed-probe --city "New York" --allow-failures`

### 1C.4 — Structured ingest bypass (skip extraction LLM)
**Files:** `scene_scout/orchestrator.py`, source adapters (`event_api.py`, `ical.py`, optional
`html_calendar.py` flag), `scene_scout/models/event.py`, tests
**Done when:**
- For `source_type` in `api`, `ical` (and optionally marked structured scrape feeds),
  orchestrator maps adapter output to `EventCandidate` or `NormalizedEvent` without calling
  `event_extraction.run()` when required fields are present
- `seen_entries` cache still keyed by `(feed_id, entry_hash)`; bypass path writes cache after
  normalization same as today
- Unit tests cover bypass vs extraction path; token savings logged in run summary (optional count)

### 1C.5 — User horizon drives time windows
**Files:** `scene_scout/normalization_config.py`, `scene_scout/pre_enrichment_filter_config.py`,
`scene_scout/agents/event_normalization.py`, `scene_scout/orchestrator.py`, iCal adapter,
tests
**Done when:**
- Normalization window and pre-enrichment "coming week" filter both use
  `profile.horizon_days` instead of hardcoded `7`
- Single source of truth passed from orchestrator (no conflicting constants)
- Unit tests: event at `now + horizon_days` kept; event at `now + horizon_days + 1` discarded

### 1C.6 — Personalization UAT and docs sync
**Files:** `docs/260705_product_redesign.md`, `README.md` (pointer), `docs/260629_uat_debug_plan.md`
(completion note only), Tier B/C examples in README or redesign doc
**Done when:**
- Personalization acceptance demo (Run A → clicks → Run B) documented and runnable
- Tier B/C examples use city + horizon aligned feeds; warn against `--max-extraction` hiding
  catalog unless testing cost caps
- Dry-run UAT produces non-zero `normalized_events` with default mainstream metro config

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
container runs the orchestrator stub. Web container starts the FastAPI onboarding UI.
Both containers resolve shared volume mounts.

### 2.7 — Batch Strategy Service
**Files:** `scene_scout/services/batch.py`
**Done when:** `get_batch_strategy(model)` returns `AnthropicBatchStrategy` for Claude
models and `ConcurrentBatchStrategy` for all others. Both implement the `BatchStrategy`
protocol. `ConcurrentBatchStrategy` is unit-testable with mocked LiteLLM calls.

### 2.8 — Cache Service
**Files:** `scene_scout/services/cache.py`
**Done when:** `CacheService` initializes all 5 SQLite tables on first use:
`feed_etags`, `seen_entries`, `performer_cache`, `venue_cache`, `vibe_cache`.

Provides the following interfaces:

**ETag cache (`feed_etags`):**
- `get_feed_etag(feed_id) -> tuple[str, str] | None` — returns `(etag, last_modified)`
- `set_feed_etag(feed_id, etag, last_modified) -> None`

**Entry deduplication cache (`seen_entries`):**
- `get_seen_entry(feed_id, entry_hash) -> NormalizedEvent | None` — returns cached
  `NormalizedEvent` if present and not expired (TTL 14 days); else `None`
- `set_seen_entry(feed_id, entry_hash, normalized_event) -> None`
- Cache key is `(feed_id, entry_hash)` — same event from different feeds gets separate
  entries to preserve source provenance

**Enrichment caches:**
- `get_performer(name_key) / set_performer(name_key, info)` — TTL 90 days
- `get_venue(venue_key) / set_venue(venue_key, ...)` — geo TTL 90 days, context TTL 30 days
- `get_vibe(content_hash) / set_vibe(content_hash, tags)` — TTL 14 days

TTL enforced on all `get` calls — expired entries return `None`. Hit/miss counts
logged per cache type per run. All interfaces unit tested including TTL expiry behavior.

### 2.9 — Mermaid Diagrams
**Files:** `docs/diagrams/diagrams.md`, `docs/diagrams/system_architecture.mmd`,
`docs/diagrams/data_flow.mmd`
**Done when:** Both diagrams render correctly in Cursor via `diagrams.md` preview.

### 2.10 — GitHub Actions CI
**Files:** `.github/workflows/ci.yml`
**Done when:** Workflow runs on every push and pull request to `main`. Steps:
`uv sync --all-extras`, then `pytest`. No live API keys required (tests use mocked
LLM and HTTP). Full UAT with real email is **not** run in CI — see Testing Conventions
and Phase 11.

**Rationale:** Automate deterministic unit and integration tests on every PR. Matches
the testing strategy in `docs/architecture.md` (CI runs mocked tests; UAT stays manual).

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

### 3.5 — seen_entries Cache Integration in Orchestrator
**Files:** `scene_scout/orchestrator.py` (extend)
**Done when:** Between Feed Scout and Extraction Agent, the orchestrator:
- Computes `entry_hash = hash(entry.link + entry.published_raw)` per `RawFeedEntry`
- Checks `cache.get_seen_entry(feed_id, entry_hash)`
- Cache hit → retrieves stored `NormalizedEvent`; entry bypasses Extraction and
  Normalization entirely; logged as cache hit at `INFO` level
- Cache miss → entry sent to Extraction Agent as normal
- After Normalization: `cache.set_seen_entry(feed_id, entry_hash, normalized_event)`
- UAT summary includes: total entries, cache hits, cache misses, hit rate percentage

---

## Phase 4 — Normalization, Deduplication, and Description Quality
*Goal: Clean, deduplicated, quality-scored NormalizedEvent records ready for enrichment.*

### 4.1 — NormalizedEvent Model
**Files:** `scene_scout/models/event.py` (extend)
**Done when:** `NormalizedEvent` includes all fields from the architecture spec:
- `id: str` — SHA-256 of title + date + venue
- `description_quality_score: float`
- `low_information: bool`
- `source_feeds: list[str]` — all feeds that provided this entry (pre-dedup)
- `source_count: int` — number of distinct source feeds
- `best_source_feed: str` — feed with highest `source_quality_score`
- `source_quality_score: float` — score of `best_source_feed`

Unit tests confirm all fields validate and default correctly.

### 4.2 — Event Normalization Agent
**Files:** `scene_scout/agents/event_normalization.py`
**Done when:** `normalization.run(candidates, run_id)`:
- Parses dates via `dateutil`
- Normalizes venue names (strip trailing punctuation, normalize whitespace)
- Standardizes categories to controlled vocabulary
- Validates URLs (format check)
- Generates stable `id` (SHA-256)
- Discards events outside the coming 7 days
- Sets `source_feeds = [candidate.source_feed]`, `source_count = 1`,
  `best_source_feed = candidate.source_feed` for each record at this stage
  (source counts are updated by the Deduplication Agent after merging)
- Unparseable dates → log + discard
- Unit tested with edge-case date strings and source field population

### 4.3 — Deduplication Agent
**Files:** `scene_scout/agents/deduplication.py`
**Done when:**
- Exact ID match collapses identical events
- `rapidfuzz` fuzzy match (title similarity > 0.85 + same date + same venue) merges
  near-duplicates
- When merging N records from different feeds, the merged record carries:
  - `source_feeds` = union of all source feed IDs
  - `source_count` = total number of distinct source feeds
  - `best_source_feed` = feed with highest `source_quality_score`
  - `source_quality_score` = score of `best_source_feed`
  - Content (title, description, venue) from the `best_source_feed` record
- All merges logged with both source feed IDs and resulting `source_count`
- Unit tested with labeled duplicate pairs including multi-feed merge scenarios

### 4.4 — Description Quality Agent
**Files:** `scene_scout/agents/description_quality.py`
**Done when:** Deterministic rubric scores all 7 signals. `description_quality_score`
and `low_information` populated on every record. `DESCRIPTION_QUALITY_THRESHOLD`
read from `config.py`. Unit tests cover every rubric boundary condition including
the performer-named signal.

### 4.5 — Pre-Enrichment Filter
**Files:** `scene_scout/orchestrator.py` (extend)
**Done when:** Filter applied after Description Quality. Discards: `low_information=True`,
outside coming week, in 2-week exclude window. Discard counts logged by reason and
included in UAT summary.

### 4.6 — CI Coverage Reporting
**Files:** `pyproject.toml` (extend), `.github/workflows/ci.yml` (extend)
**Done when:**
- `pytest-cov` added to `pyproject.toml` dev dependencies
- `ci.yml` pytest step updated to run with coverage flags:
  `pytest --cov=scene_scout --cov-report=xml --cov-report=term-missing`
- `MishaKav/pytest-coverage-comment` action added as a step after pytest; reads
  `coverage.xml` and posts a formatted coverage table as a PR comment; comment is
  updated (not duplicated) on subsequent pushes to the same PR
- `permissions: pull-requests: write` set on the workflow job
- `minimum-coverage: 80` threshold configured on the action; build passes at current
  coverage level (threshold enforced from this subphase forward)
- `coverage.xml` and `.coverage` added to `.gitignore`
- Coverage report is visible on a test PR: summary table shows per-file statement
  counts, missing lines, and overall percentage

**Rationale:** Phase 4 completes the deterministic core of the pipeline
(Phases 1–4: feed ingestion, extraction, normalization, deduplication, description
quality, pre-enrichment filter). This is the right moment to establish a coverage
baseline before Phase 5 expands the test surface with enrichment agents, geocoding,
and batch orchestration. Every Phase 5+ PR will automatically surface coverage gaps
as they are introduced.

**Note on threshold:** 80% is the enforced floor from this point forward. The initial
baseline after 4.5 may be below 80% for files like `orchestrator.py` that are harder
to unit test in isolation — review the first report and adjust the threshold to the
actual baseline, then raise it incrementally as coverage improves.

---

## Phase 5 — Enrichment Pipeline
*Goal: Enrich filtered events with performer intelligence, vibe tags, and hyper-local neighborhood context via the batch strategy.*

### 5.1 — EnrichedEvent Model
**Files:** `scene_scout/models/enrichment.py`
**Done when:** `EnrichedEvent`, `PerformerInfo` validate correctly. All enrichment
fields default to empty/null. Inherits cleanly from `NormalizedEvent` (including all
source provenance fields).

### 5.2 — Geocoding Service
**Files:** `scene_scout/services/geocoding.py`
**Done when:** `geocode_venue(venue, city)` queries Nominatim, returns `(lat, lon)` or
`None`. `get_nearby_pois(lat, lon, radius_m=1000)` returns list of POI dicts. Results
cached in `venue_cache` table with 90-day TTL. Rate limit enforced (1 req/sec). Unit
tested with mocked Nominatim responses.

### 5.3 — Talent Scout Prompt and Agent
**Files:** `scene_scout/prompts/talent_scout.txt`,
`scene_scout/agents/talent_scout.py`
**Done when:** Agent identifies named performers in event descriptions. Checks
`performer_cache` first. Submits uncached events to batch strategy. Applies results to
`EnrichedEvent.performers`. Performers with `confidence < 0.7` stored in cache but
`one_line_summary` set to `None`. On validation error per event: empty performers list.

### 5.4 — Vibe Classifier Prompt and Agent
**Files:** `scene_scout/prompts/vibe_classifier.txt`,
`scene_scout/agents/vibe_classifier.py`
**Done when:** Agent assigns 2–5 vibe tags per event from the controlled vocabulary.
Checks `vibe_cache` first. Submits uncached events to batch strategy. Tags outside
controlled vocabulary rejected — record logged and `vibe_tags` set to `[]`. Tag
distribution logged per run.

### 5.5 — Neighborhood Scout Prompt and Agent
**Files:** `scene_scout/prompts/neighborhood_scout.txt`,
`scene_scout/agents/neighborhood_scout.py`
**Done when:** Agent geocodes each venue (Mode A) or falls back to Mode B. POI list
passed to LLM as structured context — LLM does not recall businesses. Checks
`venue_cache` first. On `neighborhood_confidence < 0.5`: context set to `None`.
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
`tests/services/test_cache.py`, `tests/fixtures/golden/enrichment/`
**Done when:** Unit tests cover: cache hit (no LLM call), cache miss (LLM called),
validation error (graceful fallback), vocabulary enforcement (vibe), `seen_entries`
TTL expiry (expired entry triggers re-extraction). Golden fixtures for 5 event types
per enrichment agent.

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
**Done when:** Prompt is a valid Jinja2 template. Receives score breakdown (including
`source_coverage`) and event fields. Returns a specific, grounded explanation. Tested
manually.

### 6.5 — Ranking Agent
**Files:** `scene_scout/agents/ranking.py`
**Done when:** `ranking.run(events, profile, run_id)` computes deterministic scores for
all 9 components:
- `category_fit`, `vibe_fit`, `semantic_similarity`, `performer_affinity`, `location`,
  `novelty`, `source_quality`, `source_coverage`, `description_quality`
- `source_coverage` = `min(source_count / SOURCE_COVERAGE_MAX, 1.0)` where
  `SOURCE_COVERAGE_MAX = 3` is a named constant in `config.py`; weak positive signal
  for events covered by multiple independent feeds
- Score breakdown logged per event
- `llm.complete()` generates explanation grounded in breakdown
- Wildcard slots assigned
- On explanation `LLMValidationError`: fallback explanation used; logged as warning
- All score component weights are named constants in `config.py`

### 6.6 — Database Migration Infrastructure (Alembic)
**Files:** `pyproject.toml` (extend), `alembic.ini`, `alembic/env.py`,
`alembic/versions/0001_initial_feedback_history_schema.py`,
`scene_scout/db/models.py`, `scene_scout/db/__init__.py`
**Done when:**
- `sqlalchemy` and `alembic` added to `pyproject.toml` dependencies
- `alembic init alembic` scaffold is in place with `alembic.ini` at project root
- `scene_scout/db/models.py` declares `feedback_events` and `recommendation_history`
  tables as SQLAlchemy `Table` objects (Core, not ORM); matches the schemas that
  6.7 will implement
- `alembic/env.py` reads `DATABASE_FEEDBACK_URL` and `DATABASE_HISTORY_URL` from
  environment (defaulting to `vol-feedback/feedback.db` and `vol-history/history.db`)
  and configures SQLite batch mode (`render_as_batch=True`)
- Initial migration `0001_initial_feedback_history_schema.py` generated via
  `alembic revision --autogenerate` and verified clean
- `alembic upgrade head` runs without error against a fresh database
- `alembic downgrade base` runs without error (rollback verified)
- A `run_migrations()` helper in `scene_scout/db/__init__.py` calls
  `alembic upgrade head` programmatically; called by the orchestrator at startup
- Unit test confirms `run_migrations()` is idempotent (safe to call on every startup)

**Note on cache.db:** The existing `vol-cache/cache.db` (Phase 2.8) is **not**
migrated to Alembic. Its schema is stable and managed by `cache_schema.py`. Only
the feedback and history stores — which will evolve with the feedback loop — are
Alembic-managed. If `cache.db` schema changes are needed in the future, migrate it
then.

**SQLite batch mode:** Alembic's `batch_alter_table` context manager is required for
SQLite schema changes (SQLite does not support `ALTER COLUMN` or `DROP COLUMN`
directly). Always use it:
```python
with op.batch_alter_table("feedback_events") as batch_op:
    batch_op.add_column(sa.Column("dwell_seconds", sa.Integer()))
```

### 6.7 — Feedback Token Infrastructure
**Files:** `scene_scout/services/feedback.py`, `scene_scout/services/history.py`
**Done when:** `generate_feedback_token()` returns a UUID. `FeedbackEvent` schema
validated. `history.write_recommendations()` and `history.get_recent()` work correctly
against SQLite (schema created via Alembic migration from 6.6 — do not call
`CREATE TABLE` directly). `feedback.log_signal()` writes to `vol-feedback`.

### 6.8 — Ranking Tests
**Files:** `tests/agents/test_ranking.py`, `tests/fixtures/golden/ranking/`
**Done when:** Unit tests cover: deterministic scoring with fixed inputs, `source_coverage`
calculation for 1/2/3-source events, score component isolation, wildcard slot assignment,
explanation fallback. Golden fixtures for 3 user profiles × 5 event types.

---

## Phase 7 — Curation, Email, and Full UAT
*Goal: Final top 10 selected, email composed, pipeline wired end-to-end. Real email
delivery verified after Resend operator setup (7.6).*

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

### 7.5 — Custom Web UI: Onboarding and Profile
**Files:**
`scene_scout/web/__init__.py`,
`scene_scout/web/app.py`,
`scene_scout/web/static/index.html`,
`scene_scout/web/static/style.css`,
`scene_scout/web/static/app.js`,
`docker/web/Dockerfile` (update),
`pyproject.toml` (update `web` extra)

**Replaces:** placeholder web module (`scene_scout/gradio_app.py`, deleted)

**Context:** The prior placeholder UI could not achieve the desired visual design.
This subphase uses a lightweight FastAPI backend + fully custom
HTML/CSS/JS frontend. Validation, profile loading, and the User Preference
Agent call live in FastAPI route handlers. The frontend is a single-page
HTML application with no JavaScript framework.

**Backend — `scene_scout/web/app.py`:**
- FastAPI application with the following routes:
  - `POST /api/onboarding` — accepts `{name, email, prompt}` JSON body;
    calls `user_preference.parse_cold_start()`; returns saved `UserProfile`
    as JSON or a `{error}` response on validation failure
  - `GET /api/profile` — loads and returns current `UserProfile` as JSON;
    returns `404` with `{error}` if no profile exists
  - `GET /health` — returns `{"status": "ok"}` for container health checks
- Input validation in `validate_onboarding_inputs()`
- `LLMInfrastructureError` → HTTP 502; `LLMValidationError` → HTTP 422
- Static files served from `scene_scout/web/static/` via `StaticFiles` mount
- Auth: HTTP Basic via `WEB_PASSWORD` env var; if unset, auth is disabled (local dev default)
- `uvicorn` used as the ASGI server
- `main()` entry point reads `WEB_SERVER_PORT` (default `7860`)

**Frontend — `scene_scout/web/static/`:**

The design language is **noir supper club** — a well-lit corner booth at a
1950s jazz club. Warm, intimate, exclusive. Not dark or austere.

*Typography:*
- Headings: Cormorant Garant (serif) — loaded from Google Fonts
- Body / UI: DM Sans at weight 300 — loaded from Google Fonts
- Brand name: Cormorant Garant 500, forest green `#2A7A4B`
- Tagline lead: Cormorant Garant 500, cognac amber `#B07D3A` — "Meet Allegra."
- Tagline sub: Cormorant Garant italic, muted warm gray `#A09080` — "She finds
  the nights worth keeping."

*Color palette:*
- Background: plain warm cream `#FAF7F2` (no pattern)
- Primary text: deep warm charcoal `#2C2820`
- Forest green `#2A7A4B` — brand name, active tab indicator, input focus
  state, button border and hover fill
- Cognac amber `#B07D3A` — field labels, tagline lead
- Muted warm gray `#A09080` — inactive tabs, tagline sub, footer text
- Border / divider: `#D4C9B8`
- Input underline at rest: `#C8BAA8`

*Layout and components:*
- Two tabs: **Onboarding** and **Profile**; tab switching handled in
  vanilla JS without page reload
- Active tab: forest green underline `2px solid #2A7A4B`, green label text
- Form fields: label above in small-caps cognac amber; input as a single
  ruled underline (no box border) — `border-bottom: 1px solid #C8BAA8`;
  focus state shifts underline to forest green
- Name and email fields side-by-side in a two-column grid
- "Your taste" textarea: same ruled underline style, 4 rows
- CTA button: **"Let Allegra in →"** — outlined style at rest
  (`border: 1.5px solid #2A7A4B`, cream background, green text); fills to
  deep forest green `#1F5C38` on hover; 2px border-radius; letter-spacing
  0.12em; text uppercase; smooth `0.2s` transition
- Footer line (below button, above page bottom): thin top border `#D4C9B8`;
  DM Sans, muted warm gray, centered fine print: "One profile, stored here.
  One email a week when your picks are ready — nothing shared, nothing sold."
- Subtitle copy below tabs: *"Your name, email, and what you love.
  That's all Allegra needs."* in italic Cormorant Garant, muted warm gray
- Generous vertical rhythm throughout; no cramped spacing
- Placeholder text: italic, `#BEB0A0`

*Onboarding tab behavior (JS):*
- On submit: `POST /api/onboarding`; show inline status message on success
  or error; display returned profile fields in a read-only summary panel
  below the form
- Client-side validation mirrors backend: name, email format, prompt required

*Profile tab behavior (JS):*
- On tab activation: `GET /api/profile`; render returned `UserProfile` fields
  as a structured read-only display (stated interests, dislikes, vibe
  preferences, category weights, etc.)
- Empty state: italic message prompting user to complete onboarding

**`pyproject.toml` changes:**
- Add `fastapi>=0.111`, `uvicorn>=0.29`, `python-multipart>=0.0.9` to `[web]` extra

**`docker/web/Dockerfile` changes:**
- Update `CMD` to: `uv run uvicorn scene_scout.web.app:app --host 0.0.0.0
  --port 7860`
- Use `WEB_SERVER_PORT=7860` for the web container
- Ensure `scene_scout/web/static/` is copied into the image

**Done when:**
- `docker-compose up` starts the web container without error
- Navigating to `http://localhost:7860` renders the SceneScout onboarding
  page with the correct design: plain cream background, Cormorant Garant
  brand name in forest green, two-line tagline, ruled underline inputs,
  outlined CTA button
- Submitting valid onboarding data calls the User Preference Agent and
  displays the saved profile
- The Profile tab loads and displays the current `UserProfile`
- The "Let Allegra in" button hover state fills to deep forest `#1F5C38`
- `GET /health` returns `{"status": "ok"}`
- `WEB_PASSWORD` set → Basic Auth prompt appears before the page loads
- Placeholder `scene_scout/gradio_app.py` removed from the repository

### 7.6 — Resend & Email Delivery Setup (operator)
**Files:** `.env.example`, `README.md` (extend env table)
**Done when:** Operator has completed external Resend setup and local env is ready
for live sends. **Not required for `--dry-run` development** — defer until you can
complete account verification (e.g. email/phone confirmation).

**Operator checklist:**
1. Create a [Resend](https://resend.com) account (free tier sufficient for dev/UAT).
2. Verify a sending domain (add DNS records per Resend dashboard) **or** use Resend
   sandbox constraints for initial testing (often limited to the account owner inbox).
3. Create an API key → set `RESEND_API_KEY` in `.env`.
4. Choose a verified from-address → set `RESEND_FROM_EMAIL` (or `FROM_EMAIL`) in `.env`.
5. Set recipient inbox → `USER_EMAIL` in `.env` (v1 delivery target; Modal Secret
   `user` in production — see Phase 11).
6. Optional: set `TRACKING_BASE_URL` once Phase 8 tracking endpoints are deployed.

**Note:** Onboarding stores email on `UserProfile.email`; the Email Composer currently
sends to `USER_EMAIL` env (single-user v1). Aligning send with profile email is a
follow-up if multi-user delivery is needed.

### 7.7 — Full End-to-End UAT
**Files:** `scene_scout/cli.py` (complete), `scene_scout/orchestrator.py` (complete)
**Done when:** `uv run python -m scene_scout.cli uat --prompt "..."` runs the full
pipeline (real Feed Scout wired, no agent stubs), writes
`output/uat_{run_id}/email_preview.html` and `summary.json`, and prints the pipeline
summary table including: feeds fetched, feeds UNCHANGED (304), `seen_entries` hit rate,
entries extracted, events after each filter stage, enrichment cache hit rates, top 10
titles and scores with `source_count` visible in score breakdown. Allegra's voice is
present in the preview HTML.

**Dry-run gate (does not require 7.6):**
- `uv run python -m scene_scout.cli uat --prompt "..." --dry-run` completes without
  error; preview HTML and summary are written; Resend is not called.

**Live email gate (requires 7.6):**
- Same command **without** `--dry-run` and `DRY_RUN=false` produces a real email at
  `USER_EMAIL` with subject prefixed `[UAT {run_id}]`. Email opens correctly in an
  inbox. Tracking links are valid once Phase 8 endpoints are deployed.

Architecture rule: **if the email did not arrive, the live UAT did not pass.**

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
**Files:** `scene_scout/prompts/evaluation.txt`, `scene_scout/agents/evaluation.py`
**Done when:** `evaluation.run(recs, profile, run_id)` produces a quality report with
`overall_quality` score, flagged recommendations, and list-level issues. Report written
to `output/{run_id}/evaluation_report.json`. Runs after Email Composer in the pipeline.

### 9.3 — Evaluation Tests
**Files:** `tests/agents/test_evaluation.py`
**Done when:** Unit tests cover: generic explanation detection, missing field flagging,
category diversity check. Mocked LLM responses.

---

## Phase 10 — Observability and Dev Section
*Goal: Pipeline is fully observable. Dev Section operational in the web UI.*

### 10.1 — Structured Run Logging
**Files:** `scene_scout/logging/logger.py` (extend),
`scene_scout/orchestrator.py` (extend)
**Done when:** Every agent writes structured JSONL log entries with: `run_id`, `agent`,
`level`, `message`, `data` (counts, cache stats, score distributions). 90-day retention
enforced at pipeline start.

### 10.2 — Web Dev Section
**Files:** `scene_scout/web/app.py`, `scene_scout/web/static/` (extend)
**Done when:** Dev Section tab displays: last 5 run logs (filterable by agent/level),
feed health dashboard (last fetch per feed, ETag support flag, `seen_entries` hit rate,
post-date-filter yield), dry-run trigger with email preview, recommendation history
browser, cache inspection panel (hit rates and TTL status per cache type).

### 10.3 — LLM-as-Judge Reranking Design
**Files:** `docs/llm_reranking.md`
**Done when:** Design document describes the v2 reranking approach: deterministic
scoring → top 20 candidates → LLM editorial layer → final top 10.

### 10.4 — Regression Tests for Ranking
**Files:** `tests/regression/test_ranking_stability.py`
**Done when:** Regression tests assert that the same `EnrichedEvent` list and
`UserProfile` produce identical `score_breakdown` values across runs, including
`source_coverage` being deterministic for given `source_count` values.

### 10.5 — Prompt Improvement Workflow
**Files:** `docs/prompt_improvement.md`
**Done when:** Document describes: how to regenerate golden file fixtures, how to
compare prompt versions using the Evaluation Agent, how to run prompt regression tests,
and how to decide when a prompt change is an improvement.

---

## Phase 11 — Deployment & Continuous Delivery
*Goal: Production deployment on Modal with a clear CI/CD split. CI gates merges;
CD deploys code; full email UAT remains a manual release check.*

Phase 11 depends on Phase 7 (working pipeline and email path). Subphases 11.1 can
start earlier (Phase 2.10 covers CI alone). Do not block Phase 3–10 on Modal deploy.

### 11.1 — Modal Application Skeleton
**Files:** `scene_scout/modal_app.py`, `docs/deployment.md`
**Done when:** `modal deploy` publishes a stub app: scheduled pipeline function (cron),
web ASGI endpoint placeholder, and documented Modal Secrets mapping (`llm`, `resend`,
`user`, `web`). Persistent volumes (`vol-cache`, `vol-logs`, `vol-pipeline-state`,
etc.) mounted per `docs/architecture.md`. Local `docker-compose` (Phase 2.6) remains
the dev parity path — it is not production CD.

### 11.2 — CD Workflow (Staging / Production)
**Files:** `.github/workflows/deploy.yml`, `docs/deployment.md` (extend)
**Done when:** Documented deploy triggers:
- **PR / push to `main`:** CI only (`ci.yml` from Phase 2.10) — no auto-deploy, no
  auto-email.
- **Merge to `main` (optional):** deploy to Modal staging via `modal deploy` using
  `MODAL_TOKEN_ID` / `MODAL_TOKEN_SECRET` GitHub secrets.
- **Release tag:** deploy to Modal production; operator runs manual full UAT
  (`uv run python -m scene_scout.cli uat --prompt "..."` without `--dry-run`) and
  confirms email arrival before considering the release complete.

CD does **not** run full UAT with live email on every merge (flaky, costly, requires
live keys). `--dry-run` UAT is for dev iteration; real-email UAT is the human release
gate per `docs/architecture.md`.

### 11.3 — Deploy Smoke Test
**Files:** `.github/workflows/deploy.yml` (extend), `tests/smoke/test_modal_stub.py`
**Done when:** Post-deploy job verifies Modal app is reachable (health check or stub
invoke). Does not send email or call LLM providers.

---

## Open Items

| Item | Status | Phase |
|---|---|---|
| Curator name | ✓ Allegra | — |
| Single-user architecture | ✓ Confirmed | — |
| Batch strategy (long-running Modal function) | ✓ Confirmed | 5 |
| Agent communication (direct returns + PipelineState) | ✓ Confirmed | 2 |
| Failure handling (graceful at record, fast at infra) | ✓ Confirmed | 2 |
| LLM service (`services/llm.py`) | ✓ Confirmed | 2 |
| Prompt templating (Jinja2) | ✓ Confirmed | 2 |
| Email source of truth (Modal Secret `USER_EMAIL`) | ✓ Confirmed | 7 |
| Resend operator setup (account, domain, API key) | Deferred — see 7.6 | 7.6 |
| Log retention (90 days) | ✓ Confirmed | 10 |
| Test strategy (mock unit / golden regression) | ✓ Confirmed | 3 |
| Web UI framework (FastAPI + custom HTML/CSS/JS) | ✓ Confirmed | 7.5 |
| Feed ETag support (best-effort; fall through if unsupported) | ✓ Confirmed | 1 |
| seen_entries cache key includes feed_id (source provenance preserved) | ✓ Confirmed | 2, 3 |
| source_coverage as Ranking score component | ✓ Confirmed | 6 |
| SOURCE_COVERAGE_MAX = 3 configurable constant | ✓ Confirmed | 6 |
| Pre-enrichment filter threshold | Provisional 0.3 — tune after Phase 4 data | 5 |
| Feedback endpoint rate limiting | Known gap; acceptable for single-user | Post-launch |
| LLM model for Evaluation Agent | Open — smaller model TBD | 9 |
| Sell-out risk ML model design | Defer to Phase 9 | 9 |
| CI coverage reporting (pytest-cov + PR comment) | Planned | 4.6 |
| Database migrations (Alembic) | Planned | 6.6 |
| GitHub Actions CI (pytest on PR) | Planned | 2.10 |
| Modal deploy + CD workflow | Planned — after Phase 7 | 11 |
| Full UAT in CI | ✗ Not planned — manual release gate | 7, 11 |
| Docker Compose vs Modal CD | ✓ Separate — Compose is local dev; Modal is prod | 2.6, 11 |
| RSS-only ingestion limitation | ✓ Confirmed gap — UAT: news RSS yielded 0 extraction candidates | 1B |
| Multi-source ingestion (adapter interface) | Planned — after Phase 7.7 | 1B |
| `global_feeds.yaml` / `user_feeds.yaml` split | Aspirational — v1 uses single `config/feeds.yaml` | 1B or 10 |
| Event API platform (Eventbrite vs Songkick) | Open — decide in 1B.3 | 1B.3 |
| iCal library calendars (NYPL, BPL) | Retired in UAT-D.11 — pending official ICS endpoints | 1B.2 |
| HTML calendar scrapers | Deferred — per-site, after 1B.1–1B.3 | 1B.4 |
| Product redesign (mainstream + personalization) | Active — see [`260705_product_redesign.md`](260705_product_redesign.md) | 1C |
| `UserProfile.home_city` / `horizon_days` | Planned | 1C.1 |
| `FeedConfig.is_national` + city-scoped load | Planned — metro match **or** always-on national feeds; APIs query `home_city` | 1C.2 |
| Mainstream metro + national feed catalog | Planned | 1C.3 |
| Structured ingest bypass (skip extraction LLM) | Planned | 1C.4 |
| Eventbrite search API | Inactive — endpoint 404; org/partner API TBD; use `is_national: true` when enabled | 1C.3 |

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
- seen_entries cache key always includes feed_id
- Shared pytest fixtures in tests/conftest.py; Sandlot-themed test strings
```

Complete and verify each subphase before starting the next.
Do not combine subphases in a single Cursor instruction.

---

## Testing Conventions

Shared pytest fixtures and constants live in `tests/conftest.py`. Individual test
modules import from there rather than duplicating setup.

- **`tests/conftest.py`** — cross-cutting fixtures used by multiple test files
  (e.g. `logs_dir`, autouse `VOL_LOGS_DIR` isolation). Domain-specific helpers
  (RSS payloads, LLM mocks) stay in their test module until a second consumer
  appears.
- **Fixture strings** — use funny references to *The Sandlot* (1993). Test run IDs,
  feed names, and sample event titles should be obviously fake and never
  confusable with production data (e.g. `TEST_RUN_ID = "youre-killing-me-smalls"`).
- **`vol-logs/` isolation** — an autouse fixture redirects `VOL_LOGS_DIR` to a
  temp directory so tests never write JSONL into the real `vol-logs/` volume.
- **`vol-logs/` is gitignored** — runtime pipeline output, not source.
- **`vol-pipeline-state/` and `output/` are gitignored** — same rule.

### CI vs UAT vs CD

| Activity | When | Requires live API keys? | Sends email? |
|---|---|---|---|
| **`pytest` (CI)** | Every PR / push via GitHub Actions (Phase 2.10) | No — mocked LLM + HTTP | No |
| **UAT `--dry-run`** | Local dev, web Dev Section | Optional (pipeline may skip LLM until wired) | No — writes `email_preview.html` (Phase 7.7) |
| **Full UAT (live email)** | Manual, pre-release; requires 7.6 | Yes — LLM, Resend, feeds | Yes — real email to `USER_EMAIL` |
| **Modal CD** | Merge/tag deploy (Phase 11) | Yes — in Modal Secrets, not in repo | Only on scheduled prod run, not on every deploy |

**Why decouple email from dry-run:** Most pipeline logic (feeds → rank → compose HTML)
can be validated without Resend. Full email delivery is an external side effect — keep
it as the explicit release gate, not a per-PR CI step. See `docs/architecture.md —
UAT Mode` and `CI/CD`.

---
