# SceneScout: System Architecture

## Overview

SceneScout is a personalized, location-agnostic event discovery agent. It reads RSS feeds
from any city, extracts and normalizes event data, enriches events with performer
intelligence, vibe classification, and hyper-local neighborhood context, ranks events
against a user's taste profile, and delivers a curated weekly top 10 recommendation email.

The system is a multi-agent application where each agent owns a discrete responsibility
with defined inputs, outputs, and failure modes. SceneScout is a single-user application
and a learning project in agentic AI design, DevOps, and backend engineering.

---

## Core Engineering Philosophy

### Separate the Scaffold from the Intelligence

Every agent has two distinct layers:

**The interface layer** — inputs, outputs, schemas, error handling, logging, the contract
with the rest of the system. Built once, built well, rarely changes.

**The implementation layer** — the logic inside the agent. For v1, a simple LLM call with
a basic prompt. Later: a better prompt, retrieval augmentation, a fine-tuned model, or a
multi-step chain. The interface stays the same.

```
scene_scout/
  agents/
    vibe_classifier.py    ← interface: orchestration, logging, error handling, schema
  prompts/
    vibe_classifier.txt   ← implementation: versioned independently
```

### Build the Skeleton Before the Intelligence

1. Full pipeline wired end-to-end — all schemas defined, all agents returning valid output
2. Each agent doing something simple but correct
3. Each agent doing something good

### Failure Handling Philosophy

- **Degrade gracefully at the record level** — a malformed event, a failed enrichment
  call, or a missing performer note does not stop the pipeline. The record continues with
  empty/default values for the failed fields and a logged warning.
- **Fail fast at the infrastructure level** — an API outage, a Resend failure, or
  a Modal volume mount error halts the pipeline immediately with a clear error.

The distinction: data quality failures are expected and recoverable. Infrastructure
failures are not.

### Agent Communication Pattern

Agents communicate via **direct return values**. Each agent takes typed inputs and returns
typed outputs. The orchestrator holds state between steps.

```python
# Clean, explicit, testable
entries, reports = await feed_scout.run(configs, run_id)
candidates = await extraction.run(entries, run_id)
events = await normalization.run(candidates, run_id)
```

A lightweight `PipelineState` dataclass is used **only** at the batch boundary — where
Phase 1 results must be persisted to `vol-pipeline-state` before the batch poll begins
and Phase 2 reads them back. No shared mutable state anywhere else.

```python
@dataclass
class PipelineState:
    run_id: str
    filtered_events: list[NormalizedEvent]
    batch_id: Optional[str] = None
    phase: str = "phase_1"  # "phase_1" | "batch_submitted" | "phase_2" | "complete"
```

### Code Standards

- **Docstrings:** NumPy-style on all public functions and classes
- **Type hints:** Required on all function signatures
- **Schemas:** Pydantic v2 for all agent inputs and outputs
- **Logging:** `rich`-powered, color-coded per agent, structured JSONL for production
- **Tests:** pytest; mocked LLM calls for unit tests; golden file fixtures for prompt
  regression tests (run manually, not in CI)
- **Prompts:** Jinja2 templates; loaded and rendered via `render_prompt(name, **kwargs)`

---

## Technology Stack

| Layer | Technology | Rationale |
|---|---|---|
| UI | FastAPI + custom HTML/CSS/JS | Onboarding, profile review, dev section; HTTP Basic auth |
| Vector store | Chroma | Liked-event embeddings for semantic similarity |
| Embeddings | `sentence-transformers` (`all-MiniLM-L6-v2`) | Open-source, local, zero API cost |
| LLM abstraction | **LiteLLM** | Unified interface over any provider; model is a config value |
| LLM service | `services/llm.py` — `complete()` | Single entry point for all LLM calls |
| Default LLM | Claude via LiteLLM | Swappable via `LLM_MODEL` env var |
| Batch strategy | Provider-aware (`services/batch.py`) | Native Anthropic batch or async concurrent fallback |
| Prompt templating | **Jinja2** via `render_prompt()` | Variable injection, conditionals, loops; validated |
| Geocoding | Nominatim (OpenStreetMap) | Free, no API key; hyper-local POI within ~1 km |
| Email delivery | **Resend** | Clean API, generous free tier |
| Deployment | Modal | Scheduling, secrets, persistent volumes, web ASGI endpoint |
| Containerization | Docker (2 containers) | `pipeline` + `web`; modular, not micro-service |
| Package management | `uv` | Fast, deterministic |
| Async HTTP | `httpx` (async) | Concurrent RSS fetching |
| Schema validation | Pydantic v2 | All agent contracts |
| Terminal output | `rich` | Color-coded per-agent logs |
| Persistent stores | SQLite + JSON + YAML | Right tool per store |

### LiteLLM Configuration

```python
# scene_scout/config.py
LLM_MODEL = os.getenv("LLM_MODEL", "claude-sonnet-4-6")
LLM_API_KEY = os.getenv("LLM_API_KEY")
LLM_API_BASE = os.getenv("LLM_API_BASE", None)  # For Ollama etc.
```

Switching providers: change `LLM_MODEL` only. No agent code changes.

```
LLM_MODEL=gpt-4o                   # OpenAI
LLM_MODEL=mistral/mistral-large    # Mistral
LLM_MODEL=ollama/llama3            # Local via Ollama
LLM_MODEL=claude-sonnet-4-6       # Anthropic (default)
```

### Centralized LLM Service

All LLM calls go through a single function. No agent imports LiteLLM directly.

```python
# scene_scout/services/llm.py

async def complete(
    prompt: str,
    system: str,
    response_model: type[T],
    run_id: str,
    agent_name: str,
) -> T:
    """
    Single entry point for all LLM calls.

    Handles: retries with exponential backoff, timeout, token usage logging,
    provider error normalization, and structured output validation.

    Parameters
    ----------
    prompt : str
        The user-turn content.
    system : str
        The system prompt (rendered from a prompt file).
    response_model : type[T]
        Pydantic model to validate and parse the LLM response.
    run_id : str
        Pipeline run identifier for log correlation.
    agent_name : str
        Calling agent name for log attribution and cost tracking.

    Returns
    -------
    T
        Validated instance of response_model.

    Raises
    ------
    LLMInfrastructureError
        On API outage, auth failure, or unrecoverable provider error.
        Triggers fail-fast behavior in the orchestrator.
    LLMValidationError
        On schema mismatch or unparseable response.
        Triggers degrade-gracefully behavior at the record level.
    """
```

### Prompt Rendering

All prompts are Jinja2 templates. Loaded and rendered via a single function.

```python
def render_prompt(name: str, **kwargs) -> str:
    """
    Load and render a Jinja2 prompt template.

    Parameters
    ----------
    name : str
        Prompt file name without extension (e.g. "talent_scout").
    **kwargs
        Variables injected into the template.

    Returns
    -------
    str
        Rendered prompt string.

    Raises
    ------
    FileNotFoundError
        If the prompt file does not exist.
    jinja2.UndefinedError
        If a required template variable is missing.
    """
```

### Batch Strategy

```python
class BatchStrategy(Protocol):
    async def submit(self, requests: list[BatchRequest], run_id: str) -> str: ...
    async def poll(self, batch_id: str) -> BatchResults: ...

class AnthropicBatchStrategy:
    """Native Anthropic Batch API — true async, 50% cost reduction."""

class ConcurrentBatchStrategy:
    """Fallback — asyncio.gather() over standard LiteLLM calls."""

def get_batch_strategy(model: str) -> BatchStrategy:
    if model.startswith("claude"):
        return AnthropicBatchStrategy()
    return ConcurrentBatchStrategy()
```

### Open-Source Cost Mitigation

| Component | Approach |
|---|---|
| Embeddings | `sentence-transformers` — zero API cost |
| Feed parsing | `feedparser` — deterministic |
| Date parsing | `dateutil` — deterministic |
| Fuzzy deduplication | `rapidfuzz` — deterministic |
| Geocoding | Nominatim — free, no key required |
| Description quality | Deterministic rubric — no LLM |
| All LLM calls | LiteLLM — provider-swappable |
| Evaluation | Smaller/cheaper model via `LLM_MODEL` |

---

## Containerization

Two containers. No more.

```
scene-scout-pipeline   ← weekly scheduled job; all pipeline agents
scene-scout-web        ← FastAPI UI + feedback/tracking endpoints
```

```
docker/
  pipeline/Dockerfile
  web/Dockerfile
docker-compose.yml     ← local development only
```

---

## Package Management: uv

```bash
uv venv && source .venv/bin/activate
uv sync --all-extras
uv run pytest tests/
uv run python -m scene_scout.cli uat --prompt "..."
```

`uv.lock` committed. `pyproject.toml` is source of truth.

---

## Feed Processing: Entry Deduplication and Change Detection

RSS feeds present two efficiency problems that require explicit solutions at the
orchestration layer, not within individual agents.

### Problem 1: Unchanged Feeds (HTTP-Level)

Before parsing, the Feed Scout checks whether a feed has changed since the last fetch
using standard HTTP conditional request headers:

- `If-None-Match: {etag}` — server returns `304 Not Modified` if content is unchanged
- `If-Modified-Since: {last_modified}` — same 304 behavior based on timestamp

On a `304` response, the feed is skipped entirely: no parsing, no extraction calls,
no downstream processing. ETag and `Last-Modified` values are stored in the `feed_etags`
table in `vol-cache` after each successful fetch and sent on the next request.

Not all RSS feeds support these headers. This is best-effort. Feeds without support fall
through to normal parsing. ETag support rate is logged per feed and visible in the
web Dev Section feed health dashboard.

### Problem 2: Re-Processing Known Entries (Entry-Level)

The same event frequently appears in the same feed across multiple consecutive weekly
runs (a concert listed 3 weeks in a row). It also frequently appears across multiple
feeds simultaneously (the same event promoted by 3 LA event sources in the same week).

**The `seen_entries` cache** prevents redundant LLM extraction and normalization calls
for entries already processed in a prior run.

**Cache key:** `(feed_id, hash(link + published_raw))`

The key includes `feed_id` deliberately. The same real-world event appearing in two
different feeds gets two separate cache entries — one per source. This preserves the
source provenance information that the Deduplication Agent uses when merging records
and computing `source_count`. Collapsing cache entries across feeds would silently
lose this signal.

**Cache value:** The full `NormalizedEvent` JSON from the prior run's extraction and
normalization of this entry.

**Cache TTL:** 14 days. Long enough to skip recurring entries. Short enough that a
corrected or updated event (venue change, date correction) gets re-extracted.

**Orchestrator flow:**
```
RawFeedEntry from Feed Scout
  |
  v
Check seen_entries cache (feed_id, entry_hash)
  |-- Cache hit, not expired  --> retrieve NormalizedEvent; skip Extraction + Normalization
  |-- Cache miss or expired   --> send to Extraction Agent
                                  --> Normalization Agent
                                  --> write result to seen_entries cache
```

### Why Multiple Copies of the Same Event Are Acceptable

When the same real-world event appears in 3 feeds, the pipeline will:
1. Produce 3 separate `NormalizedEvent` records — one per feed source
2. Store 3 `seen_entries` cache entries (keyed by feed_id + entry hash)
3. Collapse all 3 into 1 merged record in the Deduplication Agent

The merged record carries `source_count: 3` and `source_feeds: ["feed_a", "feed_b", "feed_c"]`.
This cross-feed coverage is a weak positive ranking signal (`source_coverage` score component).
The incremental storage cost is negligible. The deduplication prevents any user-facing
duplication.

---

## Deployment: Modal

### Scheduled Pipeline

Single long-running Modal function. Two-phase execution:

```
Phase 1 — Ingest and normalize:
  Feed Scout (ETag/304 check → skip UNCHANGED feeds)
  → seen_entries cache check → skip known entries
  → Extraction (new entries only)
  → Normalization → Deduplication → Description Quality
  → Pre-enrichment filter
  → Geocode venues (Nominatim, cached)
  → Submit enrichment batch (Talent Scout + Vibe + Neighborhood Scout)
  → Write PipelineState to vol-pipeline-state
  → Poll every 5 minutes via asyncio.sleep()

Phase 2 — Enrich, rank, and send:
  → Read PipelineState from vol-pipeline-state
  → Apply batch results → EnrichedEvent[]
  → Ranking → Sell-Out Risk → Curator → Email Composer → Send
  → Clear vol-pipeline-state on success
```

### Secret Management

```
LLM_API_KEY    →  Modal secret: llm
RESEND_API_KEY →  Modal secret: resend
USER_EMAIL     →  Modal secret: user       ← source of truth for email delivery
USER_NAME      →  Modal secret: user       ← used in email salutation
WEB_PASSWORD   →  Modal secret: web
```

Deploy is via Modal (Phase 11), not by running Docker Compose in production. GitHub
Actions CI (Phase 2.10) runs `pytest` on every PR; CD (Phase 11.2) runs `modal deploy`
on merge/tag. See **CI/CD** under Testing Strategy.

### Persistent Volumes — One Per Store

| Volume | Contents | Owner |
|---|---|---|
| `vol-chroma` | Chroma vector index | Ranking Agent |
| `vol-feedback` | SQLite feedback log | Feedback Service |
| `vol-history` | SQLite recommendation history | Curator Agent |
| `vol-profiles` | JSON user profile | User Preference Agent |
| `vol-cache` | SQLite: seen_entries, feed_etags, performer, venue, vibe caches | Cache Service |
| `vol-pipeline-state` | `PipelineState` JSON; cleared post-run | Orchestrator |
| `vol-logs` | Structured JSONL run logs; 90-day retention | All agents |

### Log Retention

- `vol-logs`: 90-day rolling retention. Deleted at pipeline start.
- `vol-pipeline-state`: Cleared after successful Phase 2. Retained on failure.
- `vol-cache`: TTL-based expiry only. No size cap in v1.

---

## vol-cache SQLite Schema

All caching lives in a single `vol-cache` volume with one table per concern.

```sql
-- HTTP conditional request headers per feed
CREATE TABLE feed_etags (
    feed_id          TEXT PRIMARY KEY,
    etag             TEXT,
    last_modified    TEXT,
    stored_at        DATETIME NOT NULL
);

-- Processed feed entries — prevents re-extraction across runs
-- Key is (feed_id, entry_hash) so same event from different feeds gets separate entries
CREATE TABLE seen_entries (
    feed_id               TEXT NOT NULL,
    entry_hash            TEXT NOT NULL,     -- hash(link + published_raw)
    normalized_event_json TEXT NOT NULL,     -- full NormalizedEvent JSON
    first_seen_at         DATETIME NOT NULL,
    expires_at            DATETIME NOT NULL, -- first_seen_at + 14 days
    PRIMARY KEY (feed_id, entry_hash)
);

-- Performer enrichment
CREATE TABLE performer_cache (
    performer_name_key    TEXT PRIMARY KEY,  -- normalized lowercase
    performer_info_json   TEXT NOT NULL,
    cached_at             DATETIME NOT NULL,
    expires_at            DATETIME NOT NULL  -- +90 days
);

-- Venue geocoding and neighborhood context
CREATE TABLE venue_cache (
    venue_key                TEXT PRIMARY KEY, -- normalized venue_name + city
    coordinates_json         TEXT,             -- {"lat": float, "lon": float}
    poi_list_json            TEXT,             -- list of nearby POI dicts within 1km
    neighborhood_context     TEXT,
    neighborhood_confidence  REAL,
    cached_at                DATETIME NOT NULL,
    geo_expires_at           DATETIME NOT NULL,     -- +90 days
    context_expires_at       DATETIME NOT NULL      -- +30 days
);

-- Vibe tag cache
CREATE TABLE vibe_cache (
    content_hash    TEXT PRIMARY KEY,  -- SHA-256(description + categories)
    vibe_tags_json  TEXT NOT NULL,
    cached_at       DATETIME NOT NULL,
    expires_at      DATETIME NOT NULL  -- +14 days
);
```

---

## UAT Mode

UAT mode runs the full pipeline end-to-end and **sends a real email** to `USER_EMAIL`.
If the email did not arrive, the test did not pass.

```bash
uv run python -m scene_scout.cli uat --prompt "I love experimental music and independent
film. I'm in Silver Lake. No corporate events."

# Pipeline only — no email sent
uv run python -m scene_scout.cli uat --prompt "..." --dry-run
```

**UAT behavior:**
- Sends real email; subject prefixed `[UAT {run_id}]`
- Runs synchronously (batch polling is inline)
- Emits verbose color-coded `rich` logs to terminal
- Writes `output/uat_{run_id}/email_preview.html`
- Writes `output/uat_{run_id}/summary.json` including:
  - Entry counts per feed
  - Feeds skipped via 304 (UNCHANGED)
  - seen_entries cache hit rate
  - Entries sent to extraction vs. retrieved from cache
  - Post-filter counts, enrichment cache hit rates, top 10 titles and scores
- Prints final summary table on completion

**Log color scheme:**

| Agent / Service | Color |
|---|---|
| Orchestrator | White |
| Feed Scout | Cyan |
| Event Extraction | Blue |
| Event Normalization | Blue |
| Deduplication | Blue |
| Description Quality | Yellow |
| Geocoding | Yellow |
| Talent Scout | Magenta |
| Vibe Classifier | Magenta |
| Neighborhood Scout | Magenta |
| Ranking | Green |
| Sell-Out Risk | Green |
| Recommendation Curator (Allegra) | Gold |
| Email Composer | Gold |
| Evaluation | Red |
| Cache / LLM Service | Dim white |

**Log levels:**

| Level | Content |
|---|---|
| `INFO` | Agent start/complete, entry counts, cache hit rates (ETag + seen_entries), send confirmation |
| `WARNING` | Feed failures, UNCHANGED feeds summary, low-confidence enrichments, sub-10 candidates |
| `ERROR` | Schema failures, API errors, send failures |
| `DEBUG` | Per-record detail; `--verbose` flag only |

---

## Run Identity

```python
run_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
# Example: "20250606-143022"
```

Generated once by orchestrator. Passed explicitly to every agent and service call.
Never a global.

---

## The Curator: Allegra

**Name etymology:** Italian/Latin — "joyful," "lively." Musical resonance: *allegro*.
Internationally legible. Works in any city.

**Character brief:** Allegra has her own creative practice — she doesn't advertise it.
She knows the bookers, the gallerists, the programmers in whatever city the user is in.
Her tone is warm but never gushing, direct but never terse. Location-agnostic — equally
at home in Los Angeles, London, Tokyo, or Berlin.

**Voice rules:**
- First person, addressed directly to the user
- Warm and specific — never promotional
- Never uses the word "curated"
- Fewer than 10 events: says so plainly, no apology, no padding

```python
class CuratorConfig(BaseModel):
    name: str = "Allegra"
    voice_brief: str  # loaded from prompts/curator_voice.txt
```

---

## User Onboarding

On first use, the user provides via the web UI:
1. **Name** — used in email salutation
2. **Email address** — stored in Modal Secrets as `USER_EMAIL`; copied to `UserProfile`
3. **Cold-start prompt** — free-text interests, dislikes, and constraints

---

## Personalization Model

### Phase 1 — Cold Start
Prompt parsed into `UserProfile`. Bootstraps the ranker immediately.

### Phase 2 — Warm Personalization

| Signal | Type | Weight delta |
|---|---|---|
| Link click | Weak positive | +0.03 per category |
| "Not for me" | Negative | −0.05 per category |
| Future: "I went" | Strong positive | +0.10 per category |

Stated preferences → hard constraints. Revealed → soft adjustments within constraints.

### Personalization Strategies (priority order)
1. Category weight decay — `e^(-λt)`, half-life ~30 days
2. Chroma embedding similarity — cosine similarity to liked events
3. Implicit non-negative signal — unflagged exposure → small upward pressure
4. Periodic LLM profile revision (v2) — user approves diff in the web UI
5. Wildcard slot — 1–2 moderate-fit, high-novelty slots per week

---

## Description Quality Scoring

Deterministic weighted rubric. No LLM in v1.

```python
DESCRIPTION_QUALITY_THRESHOLD: float = 0.3  # configurable in config.py
```

| Signal | Weight | Measurement |
|---|---|---|
| Description length | 0.20 | 0 chars → 0.0 / <50 → 0.3 / 50–150 → 0.7 / 150+ → 1.0 |
| Venue presence | 0.20 | Non-null, non-generic → 1.0; else 0.0 |
| Date + time present | 0.20 | Both → 1.0 / date only → 0.5 / neither → 0.0 |
| Performer/artist named | 0.15 | Named performer in title or description → 1.0; else 0.0 |
| Category coverage | 0.10 | ≥1 non-generic category → 1.0; else 0.0 |
| URL validity | 0.10 | Present and well-formed → 1.0; else 0.0 |
| Price clarity | 0.05 | `price_cents` set OR `is_free=True` → 1.0; else 0.0 |

Applied as a confidence discount in Ranking, not a hard filter.

---

## Neighborhood Scout: Hyper-Local Architecture

Strict **15–20 minute walking radius** (~1 km) from the venue.

**Mode A — Geocoding-assisted (primary):** Nominatim geocodes venue → `(lat, lon)` →
POIs within ~1 km → passed as structured context to LLM. LLM narrates what it is given.

**Mode B — LLM-only fallback:** General neighborhood character note only. No specific
businesses. Honest and safe.

**Cache:** `venue_cache` table. Geocoding TTL 90 days. Context TTL 30 days.

---

## Vibe Classifier Vocabulary

Fixed. 2–5 tags per event. No tags outside this list.

```
intimate        high-energy     experimental    social          introspective
outdoor         late-night      family-friendly industry        touristy
immersive       underground     high-production free-spirited   niche
single-friendly pretentious     exclusive       inclusive
```

Quality check: any tag exceeding 40% of events in a run → classifier collapse.

---

## Ranking Agent: Design Rationale

**Deterministic scoring** for score computation. **LLM generation** for explanations only.

**Score components:**
- `category_fit` — overlap with `UserProfile.category_weights`
- `vibe_fit` — overlap with `UserProfile.vibe_preferences`
- `semantic_similarity` — cosine similarity to Chroma liked-events (0.0 cold start)
- `performer_affinity` — `top_performer_affinity` from Talent Scout
- `location` — proximity to user's preferred neighborhoods
- `novelty` — exponential recency decay (14–28 days since last recommendation) plus
  exploration bonus for unseen categories/vibes; hard exclude within 14 days
- `source_quality` — from feed config (`best_source_feed.source_quality_score`)
- `source_coverage` — normalized `source_count`; weak positive for multi-feed events
- `description_quality` — confidence discount for `low_information` records

All components normalized to 0.0–1.0. Composite is a weighted sum. Weights are named
constants in `config.py`.

**v2 path — LLM-as-judge reranking (Phase 10):** Deterministic top 20 → LLM editorial
layer → final top 10. On top of scores, not instead of them.

---

## Testing Strategy

| Test Type | Scope | Fixture Approach | Runs In |
|---|---|---|---|
| Unit tests | Agent logic, deterministic components, services | Mocked LLM + HTTP | CI |
| Integration tests | Agent-to-agent data flow; cache behavior | Mocked LLM; real SQLite | CI |
| Prompt regression | LLM output quality; schema validity | Golden file fixtures | Manually |
| UAT | Full end-to-end pipeline; real email | Live API calls | Manually |

**CI scope (Phase 2.10):** `uv sync --all-extras` + `pytest` on every push/PR. Tests
use mocked LiteLLM and HTTP (respx); autouse fixtures in `tests/conftest.py` isolate
runtime volumes. No secrets required in GitHub Actions for the default CI job.

**Not in CI:** prompt regression (golden files, manual), full UAT with real email,
Modal deploy. These are manual or release-gate activities — see CI/CD below.

---

## CI/CD

SceneScout separates **continuous integration** (merge gates), **continuous delivery**
(deploy code to Modal), and **acceptance testing** (full UAT with real email).

### What runs where

| Trigger | Action | Workflow |
|---|---|---|
| PR / push to `main` | CI: install deps, run `pytest` | `.github/workflows/ci.yml` (Phase 2.10) |
| Merge to `main` (optional) | CD: `modal deploy` to staging | `.github/workflows/deploy.yml` (Phase 11.2) |
| Release tag | CD: `modal deploy` to production | Phase 11.2 |
| Pre-release (human) | Full UAT without `--dry-run`; confirm inbox | Not automated |

### Local dev vs production deploy

| Environment | Mechanism | Purpose |
|---|---|---|
| **Local** | `uv run`, `docker-compose up` (Phase 2.6) | Day-to-day development; volume mounts mirror prod layout |
| **Production** | Modal — scheduled pipeline + web ASGI endpoint (Phase 11) | Cron job, secrets, persistent volumes |

Docker Compose is **dev parity**, not production CD. Do not conflate `docker-compose up`
with `modal deploy` — same codebase, different entrypoints, secrets, and schedulers.

### UAT modes and release gate

```bash
# Dev iteration — pipeline + preview, no email send
uv run python -m scene_scout.cli uat --prompt "..." --dry-run

# Release gate — full end-to-end including real email (manual)
uv run python -m scene_scout.cli uat --prompt "..."
```

Architecture rule: **if the email did not arrive, the UAT did not pass.** That check
stays manual (or post-deploy operator step), not a GitHub Actions job on every PR.

### Secrets

| Secret location | Used by |
|---|---|
| `.env` (local, gitignored) | Local dev; `DRY_RUN=true` by default in `.env.example` |
| GitHub Actions secrets | `MODAL_TOKEN_ID`, `MODAL_TOKEN_SECRET` for CD only (Phase 11) |
| Modal Secrets | `LLM_API_KEY`, `RESEND_API_KEY`, `USER_EMAIL`, `WEB_PASSWORD` at runtime |

Never commit API keys. CI pytest job must pass without them.

See `docs/project_plan.md` — Phase 2.10 (CI), Phase 11 (Modal deploy & CD).

---

## Web UI

FastAPI application (`scene_scout/web/app.py`) with a custom HTML/CSS/JS frontend.
HTTP Basic auth when `WEB_PASSWORD` is set (username `scenescout`).

**User-facing:** Onboarding, profile viewer/editor, feedback history, feed management.

**Dev Section:** Run log viewer (filter by `run_id`, agent, level), feed health dashboard
(ETag support coverage, seen_entries hit rates, yield per feed), global feed management,
dry-run trigger, recommendation history browser, cache inspection.

---

## Agent Roster

### 1. Feed Scout Agent

RSS-only in Phase 1. **Phase 1B** adds pluggable source adapters (`rss`, `ical`, `api`,
`scrape`) that all normalize to `RawFeedEntry` — see `docs/project_plan.md` Phase 1B.
The agent name may evolve to Source Scout when dispatch is added; the orchestrator
contract stays the same.

| | |
|---|---|
| Inputs | `list[FeedConfig]`, `run_id: str` |
| Outputs | `tuple[list[RawFeedEntry], list[FeedHealthReport]]` |
| LLM | No |
| Deterministic | Yes |
| Log color | Cyan |
| Failure handling | Per-feed: log + skip. Never halts pipeline. |

Reads full feed snapshot (typically 10–50 entries). No artificial cap. Temporal scoping
handled by Normalization Agent's 7-day date filter downstream.

Sends `If-None-Match` / `If-Modified-Since` on every request. On `304`, status is
`UNCHANGED`; feed is skipped. ETag/Last-Modified stored in `feed_etags` after each
successful fetch.

```python
class FeedStatus(str, Enum):
    OK          = "ok"
    UNCHANGED   = "unchanged"    # 304 Not Modified; skipped cleanly
    UNREACHABLE = "unreachable"
    MALFORMED   = "malformed"
    EMPTY       = "empty"
    STALE       = "stale"

class FeedConfig(BaseModel):
    id: str
    name: str
    url: str
    city: str
    source_quality_score: float
    active: bool = True
    notes: Optional[str] = None
    cursor: Optional[str] = None  # Always None for RSS; reserved for cursor-based APIs
    # Phase 1B (planned): source_type: Literal["rss", "ical", "api", "scrape"] = "rss"
```

---

### 2. Event Extraction Agent

| | |
|---|---|
| Inputs | `list[RawFeedEntry]`, `run_id: str` |
| Outputs | `list[EventCandidate]` |
| LLM | Yes — via `llm.complete()` |
| Log color | Blue |
| Failure handling | Per-entry: log + skip on `LLMValidationError` |

The orchestrator checks `seen_entries` before invoking this agent. Only cache-miss
entries reach the Extraction Agent. Cache hits return stored `NormalizedEvent` records
directly, bypassing both extraction and normalization.

```python
class EventCandidate(BaseModel):
    title: str
    date: Optional[str]
    time: Optional[str]
    venue: Optional[str]
    neighborhood: Optional[str]
    city: str
    url: str
    price: Optional[str]
    description: Optional[str]
    categories: list[str]
    is_event: bool
    extraction_confidence: float
    source_feed: str
    run_id: str
    extracted_at: datetime
```

---

### 3. Event Normalization Agent

| | |
|---|---|
| Inputs | `list[EventCandidate]`, `run_id: str` |
| Outputs | `list[NormalizedEvent]` |
| LLM | Sparingly via `llm.complete()` |
| Deterministic | Yes |
| Log color | Blue |
| Failure handling | Per-record: unparseable date → log + discard |

```python
class NormalizedEvent(BaseModel):
    id: str                         # SHA-256: title + date + venue
    title: str
    start_datetime: datetime
    end_datetime: Optional[datetime]
    venue: str
    neighborhood: Optional[str]
    city: str
    url: str
    price_cents: Optional[int]
    is_free: bool
    description: str
    categories: list[str]
    source_feeds: list[str]         # All feeds that provided this entry (pre-dedup)
    source_count: int               # Number of distinct source feeds
    best_source_feed: str           # Feed with highest source_quality_score
    source_quality_score: float     # Score of best_source_feed
    description_quality_score: float
    low_information: bool
    run_id: str
    normalized_at: datetime
```

---

### 4. Deduplication Agent

| | |
|---|---|
| Inputs | `list[NormalizedEvent]`, `run_id: str` |
| Outputs | Deduplicated `list[NormalizedEvent]`; merge log |
| LLM | Optional escalation |
| Log color | Blue |
| Escalation | exact ID → fuzzy (`rapidfuzz`) → embedding → LLM |

When records from multiple feeds are merged, the output carries `source_feeds` as the
union of all source IDs, `source_count` as the total distinct sources, and
`best_source_feed`/`source_quality_score` from the highest-quality source. Cross-feed
coverage is preserved through to ranking as the `source_coverage` score component.

---

### 5. Description Quality Agent

| | |
|---|---|
| Inputs | `list[NormalizedEvent]`, `run_id: str` |
| Outputs | `list[NormalizedEvent]` with `description_quality_score`, `low_information` |
| LLM | No (v1) |
| Log color | Yellow |
| Threshold | `DESCRIPTION_QUALITY_THRESHOLD = 0.3` in `config.py` |

---

### 6. Talent Scout Agent

| | |
|---|---|
| Inputs | `list[NormalizedEvent]`, `UserProfile`, `run_id: str` |
| Outputs | `list[EnrichedEvent]` with `performers` |
| LLM | Yes — via batch strategy |
| Log color | Magenta |
| Cache | `performer_cache` table; key: normalized performer name; TTL 90 days |
| Failure handling | Per-event: validation error → empty `performers`; log warning |

```python
class PerformerInfo(BaseModel):
    name: str
    entity_type: str           # "musician"|"band"|"filmmaker"|"speaker"|"artist"|"other"
    genre_tags: list[str]
    one_line_summary: Optional[str]
    confidence: float          # Surface only if >= 0.7
    affinity_score: float

class EnrichedEvent(NormalizedEvent):
    performers: list[PerformerInfo] = []
    top_performer_affinity: float = 0.0
    vibe_tags: list[str] = []
    neighborhood_context: Optional[str] = None
    neighborhood_confidence: float = 0.0
    venue_coordinates: Optional[tuple[float, float]] = None
```

---

### 7. Vibe Classifier Agent

| | |
|---|---|
| Inputs | `list[EnrichedEvent]`, `run_id: str` |
| Outputs | `list[EnrichedEvent]` with `vibe_tags` |
| LLM | Yes — via batch strategy |
| Log color | Magenta |
| Cache | `vibe_cache` table; key: SHA-256(description + categories); TTL 14 days |
| Failure handling | Per-event: validation error → empty `vibe_tags`; log warning |

---

### 8. Neighborhood Scout Agent

| | |
|---|---|
| Inputs | `list[EnrichedEvent]`, `UserProfile`, `run_id: str` |
| Outputs | `list[EnrichedEvent]` with `neighborhood_context` |
| LLM | Yes — narrates geocoded POI data; via batch strategy |
| Log color | Magenta |
| Cache | `venue_cache` table; geo TTL 90 days, context TTL 30 days |
| Failure handling | Per-event: geocoding fail or confidence < 0.5 → `None`; log warning |

---

### 9. User Preference Agent

| | |
|---|---|
| Inputs | Onboarding data (first run); feedback signals (subsequent runs); `run_id` |
| Outputs | `UserProfile` written to `vol-profiles` |
| LLM | Yes — via `llm.complete()` |
| Log color | White |

```python
class UserProfile(BaseModel):
    user_id: str
    name: str
    email: str
    stated_interests: list[str]
    stated_dislikes: list[str]
    preferred_neighborhoods: list[str]
    max_travel_minutes: Optional[int]
    budget_ceiling_cents: Optional[int]
    excluded_categories: list[str]
    category_weights: dict[str, float]
    vibe_preferences: list[str]
    created_at: datetime
    last_updated: datetime
    profile_version: int
```

---

### 10. Ranking Agent

| | |
|---|---|
| Inputs | `list[EnrichedEvent]`, `UserProfile`, Chroma index, `run_id: str` |
| Outputs | `list[RankedEvent]` |
| LLM | Yes — explanations only |
| Deterministic | Yes — scoring is reproducible |
| Log color | Green |
| Failure handling | Explanation failure → generic fallback; log warning |

```python
class RankedEvent(BaseModel):
    event: EnrichedEvent
    score: float
    score_breakdown: dict[str, float]
    # Keys: category_fit, vibe_fit, semantic_similarity, performer_affinity,
    #       location, novelty, source_quality, source_coverage, description_quality
    explanation: str
    is_previously_recommended: bool
    novelty_penalty_applied: bool
    wildcard_slot: bool
    run_id: str
```

---

### 11. Sell-Out Risk Agent

| | |
|---|---|
| Inputs | `list[RankedEvent]`, `run_id: str` |
| Outputs | `list[RankedEvent]` with `sellout_risk` |
| LLM | Optional |
| Deterministic | Yes — heuristic classifier |
| Log color | Green |

---

### 12. Recommendation Curator Agent (Allegra)

| | |
|---|---|
| Inputs | `list[RankedEvent]`, `UserProfile`, history, `run_id: str` |
| Outputs | `list[CuratedRecommendation]` (up to 10) |
| LLM | Optional |
| Deterministic | Yes |
| Log color | Gold |
| Sub-10 | Sends what passed; Allegra's note explains plainly |

```python
class CuratedRecommendation(BaseModel):
    rank: int
    event: EnrichedEvent
    score: float
    score_breakdown: dict[str, float]
    explanation: str
    neighborhood_context: Optional[str]
    sellout_risk: str
    sellout_urgency_note: Optional[str]
    feedback_token: str
    is_wildcard: bool
    run_id: str
    recommended_at: datetime
```

**Diversity rules (v1):** Max 3 events per category · Max 2 per venue · ≥2 different
dates · 1–2 wildcard slots · Soft recency (14–28 days): exponential novelty decay ·
Last 2 weeks: hard exclude

---

### 13. Email Composer Agent

| | |
|---|---|
| Inputs | `list[CuratedRecommendation]`, `UserProfile`, `run_id: str` |
| Outputs | Rendered HTML; Resend send confirmation |
| LLM | Yes |
| Log color | Gold |
| Email address | `USER_EMAIL` Modal Secret |
| Failure handling | Resend failure → `LLMInfrastructureError`; pipeline halts |

---

### 14. Evaluation Agent

| | |
|---|---|
| Inputs | Run logs, recommendations, feedback, `UserProfile`, `run_id` |
| Outputs | Quality report; flagged issues |
| LLM | Yes — smaller model |
| Log color | Red |

---

## Persistent Stores

| Store | Volume | Format | Owner | Retention |
|---|---|---|---|---|
| Global feeds | repo | YAML | Operator | Permanent |
| User feeds | repo | YAML | User | Permanent |
| User profile | `vol-profiles` | JSON | User Preference Agent | Permanent |
| Chroma index | `vol-chroma` | Chroma DB | Ranking Agent | Permanent |
| Feedback events | `vol-feedback` | SQLite | Feedback Service | Permanent |
| Recommendation history | `vol-history` | SQLite | Curator Agent | Permanent |
| All caches | `vol-cache` | SQLite (5 tables) | Cache Service | TTL-based |
| Pipeline state | `vol-pipeline-state` | JSON | Orchestrator | Cleared post-run |
| Run logs | `vol-logs` | JSONL | All agents | 90 days rolling |

---

## Repository Structure

```
scene-scout/
├── .github/
│   └── workflows/
│       ├── ci.yml                    ← pytest on PR/push (Phase 2.10)
│       └── deploy.yml                ← modal deploy on merge/tag (Phase 11.2)
├── docker/
│   ├── pipeline/Dockerfile
│   └── web/Dockerfile
├── docker-compose.yml
├── pyproject.toml
├── uv.lock
├── .env.example
├── config/
│   ├── feeds.yaml                  ← v1: single operator-edited source list (Phase 1)
│   ├── global_feeds.yaml           ← aspirational: shipped defaults (Phase 1B or 10)
│   └── user_feeds.yaml             ← aspirational: user-added sources via web UI
├── docs/
│   ├── architecture.md
│   ├── deployment.md                 ← Modal secrets, CD triggers (Phase 11)
│   ├── project_plan.md
│   └── diagrams/
│       ├── diagrams.md
│       ├── system_architecture.mmd
│       └── data_flow.mmd
├── output/                           ← UAT output; gitignored
├── scene_scout/
│   ├── __init__.py
│   ├── cli.py
│   ├── web/                          ← FastAPI UI (Phase 7.5)
│   │   ├── app.py
│   │   └── static/
│   ├── modal_app.py                  ← Modal entrypoint (Phase 11.1)
│   ├── orchestrator.py               ← run_id; PipelineState; seen_entries check
│   ├── config.py
│   ├── curator_config.py
│   ├── agents/
│   │   ├── feed_scout.py             ← ETag/304 + FeedStatus.UNCHANGED
│   │   ├── event_extraction.py
│   │   ├── event_normalization.py    ← source_feeds, source_count, best_source_feed
│   │   ├── deduplication.py          ← multi-source merge logic
│   │   ├── description_quality.py
│   │   ├── talent_scout.py
│   │   ├── vibe_classifier.py
│   │   ├── neighborhood_scout.py
│   │   ├── user_preference.py
│   │   ├── ranking.py                ← source_coverage score component
│   │   ├── sellout_risk.py
│   │   ├── recommendation_curator.py
│   │   ├── email_composer.py
│   │   └── evaluation.py
│   ├── models/
│   │   ├── feed.py                   ← FeedConfig.cursor; FeedStatus.UNCHANGED
│   │   ├── event.py                  ← NormalizedEvent source provenance fields
│   │   ├── enrichment.py
│   │   ├── ranking.py                ← source_coverage in score_breakdown
│   │   └── user.py
│   ├── prompts/
│   │   ├── curator_voice.txt
│   │   ├── email_composer.txt
│   │   ├── evaluation.txt
│   │   ├── event_extraction.txt
│   │   ├── neighborhood_scout.txt
│   │   ├── ranking_explanation.txt
│   │   ├── talent_scout.txt
│   │   ├── user_preference_parse.txt
│   │   └── vibe_classifier.txt
│   ├── services/
│   │   ├── batch.py
│   │   ├── cache.py                  ← seen_entries, feed_etags, all enrichment caches
│   │   ├── chroma.py
│   │   ├── feedback.py
│   │   ├── geocoding.py
│   │   ├── history.py
│   │   ├── llm.py
│   │   └── prompt_loader.py
│   └── logging/
│       └── logger.py
└── tests/
    ├── fixtures/
    │   └── golden/
    ├── agents/
    ├── models/
    └── services/
```

---

## Design Principles

1. Separate the scaffold from the intelligence.
2. Build the skeleton before the intelligence.
3. LiteLLM is the only LLM interface — no direct provider SDK imports in agents.
4. All LLM calls go through `services/llm.py` — one entry point, consistent behavior.
5. Prompts are Jinja2 templates in files — versioned independently of agent code.
6. Degrade gracefully at the record level; fail fast at the infrastructure level.
7. Agents communicate via direct return values — no shared mutable state except `PipelineState` at the batch boundary.
8. Neighborhood Scout narrates facts it is given — it does not recall geography.
9. Scoring is deterministic; explanation is generative — never conflate them.
10. UAT sends a real email — if it didn't arrive, the test didn't pass.
11. Multiple feed copies of the same event are acceptable — deduplication is downstream, not at ingestion.
12. `seen_entries` cache key includes `feed_id` — source provenance is never collapsed at the cache layer.
13. Log with intent — color-coded, structured, traceable by `run_id`.
14. NumPy-style docstrings on all public functions and classes.
15. Add tests where they protect core behavior; golden files for prompt regression.
16. Every major component must answer: "Is this working?"
