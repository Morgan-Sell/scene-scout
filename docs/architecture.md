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
- **Fail fast at the infrastructure level** — a Claude API outage, a Resend failure, or
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
| UI | Gradio (built-in auth) | Python-native UI; cold-start, profile review, dev section |
| Vector store | Chroma | Liked-event embeddings for semantic similarity |
| Embeddings | `sentence-transformers` (`all-MiniLM-L6-v2`) | Open-source, local, zero API cost |
| LLM abstraction | **LiteLLM** | Unified interface over any provider; model is a config value |
| LLM service | `services/llm.py` — `complete()` | Single entry point for all LLM calls |
| Default LLM | Claude via LiteLLM | Swappable via `LLM_MODEL` env var |
| Batch strategy | Provider-aware (`services/batch.py`) | Native Anthropic batch or async concurrent fallback |
| Prompt templating | **Jinja2** via `render_prompt()` | Variable injection, conditionals, loops; validated |
| Geocoding | Nominatim (OpenStreetMap) | Free, no API key; hyper-local POI within ~1 km |
| Email delivery | **Resend** | Clean API, generous free tier |
| Deployment | Modal | Scheduling, secrets, persistent volumes, Gradio serving |
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
LLM_MODEL=claude-sonnet-4-6      # Anthropic (default)
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
# scene_scout/services/prompt_loader.py

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
# scene_scout/services/batch.py

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
scene-scout-web        ← Gradio UI + feedback/tracking endpoints
```

```
docker/
  pipeline/Dockerfile
  web/Dockerfile
docker-compose.yml     ← local development only
```

Pipeline and web are split because they have different runtime profiles: pipeline runs
once weekly for minutes; web runs continuously and must respond in milliseconds.

---

## Package Management: uv

```bash
uv venv && source .venv/bin/activate
uv sync --all-extras

uv add litellm resend jinja2 nominatim
uv add --dev pytest respx

uv run pytest tests/
uv run python -m scene_scout.cli uat --prompt "..."
```

`uv.lock` committed. `pyproject.toml` is source of truth.

---

## Deployment: Modal

### Scheduled Pipeline

Single long-running Modal function. Two-phase execution within one function:

```
Phase 1 — Ingest and normalize:
  Feed Scout → Extraction → Normalization → Deduplication → Description Quality
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
```

`UserProfile.email` stores a copy for reference and logging. Email Composer reads from
Modal Secret, not the profile, to prevent stale profile data causing misdelivery.

### Persistent Volumes — One Per Store

| Volume | Contents | Owner |
|---|---|---|
| `vol-chroma` | Chroma vector index | Ranking Agent |
| `vol-feedback` | SQLite feedback log | Feedback Service |
| `vol-history` | SQLite recommendation history | Curator Agent |
| `vol-profiles` | JSON user profile | User Preference Agent |
| `vol-cache` | SQLite enrichment + geocoding cache | Enrichment agents |
| `vol-pipeline-state` | `PipelineState` JSON; cleared post-run | Orchestrator |
| `vol-logs` | Structured JSONL run logs; 90-day retention | All agents |

### Log Retention

- `vol-logs`: 90-day rolling retention. Logs older than 90 days are deleted at the
  start of each pipeline run.
- `vol-pipeline-state`: Cleared immediately after successful Phase 2 completion.
  Retained on failure for debugging and potential manual resume.
- `vol-cache`: TTL-based expiry only. No size cap in v1.

---

## UAT Mode

UAT mode runs the full pipeline end-to-end and **sends a real email** to `USER_EMAIL`.
This is a true end-to-end test. If the email did not arrive, the test did not pass.

```bash
uv run python -m scene_scout.cli uat --prompt "I love experimental music and independent
film. I'm in Silver Lake. No corporate events."

# Pipeline only — no email sent
uv run python -m scene_scout.cli uat --prompt "..." --dry-run
```

**UAT behavior:**
- Sends real email to `USER_EMAIL`; subject prefixed `[UAT {run_id}]`
- Runs synchronously (batch polling is inline)
- Emits verbose color-coded `rich` logs to terminal
- Writes `output/uat_{run_id}/email_preview.html` for browser inspection
- Writes `output/uat_{run_id}/summary.json`
- Prints final summary table on completion

**`--dry-run`** skips email send; writes preview file only.

**Log color scheme:**

| Agent | Color |
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
| `INFO` | Agent start/complete, record counts, cache rates, send confirmation |
| `WARNING` | Feed failures, low-confidence enrichments, sub-10 candidates |
| `ERROR` | Schema failures, API errors, send failures |
| `DEBUG` | Per-record detail; `--verbose` flag only |

---

## Run Identity

```python
run_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
# Example: "20250606-143022"
```

Generated once by orchestrator. Passed explicitly to every agent and service call.
Never a global. Attached to all logs, entries, recommendations, and feedback events.

---

## The Curator: Allegra

SceneScout's recommendation curator is **Allegra**.

**Name etymology:** Italian/Latin — "joyful," "lively." Musical resonance: *allegro*.
Internationally legible. Works in any city. The musical meaning fits a curator of live
events precisely — and it's subtle enough that most users won't consciously register it.

**Character brief:**
Allegra has her own creative practice — she doesn't advertise it. She knows the bookers,
the gallerists, the programmers in whatever city the user is in. She goes to things because
she genuinely wants to, not because she's working. Her tone is warm but never gushing,
direct but never terse. She makes you feel like you're being let in on something.
She is location-agnostic — equally at home recommending events in Los Angeles, London,
Tokyo, or Berlin.

**Voice rules (enforced in `prompts/curator_voice.txt`):**
- First person, addressed directly to the user
- Warm and specific — never promotional
- Never uses the word "curated"
- When fewer than 10 events pass: says so plainly, no apology, no padding

```python
class CuratorConfig(BaseModel):
    name: str = "Allegra"
    voice_brief: str  # loaded from prompts/curator_voice.txt
```

---

## User Onboarding

On first use, the user provides three things via the Gradio UI:

1. **Name** — used in email salutation (`Dear {name},`)
2. **Email address** — stored in Modal Secrets as `USER_EMAIL`; copied to `UserProfile`
3. **Cold-start prompt** — free-text description of interests, dislikes, and constraints

The User Preference Agent parses the prompt into a structured `UserProfile`. The name
and email are stored immediately. No other onboarding is required.

---

## Personalization Model

### Phase 1 — Cold Start
User submits name, email, and free-text prompt. User Preference Agent parses prompt into
`UserProfile`. Bootstraps the ranker immediately.

### Phase 2 — Warm Personalization

| Signal | Type | Weight delta |
|---|---|---|
| Link click | Weak positive | +0.03 per category |
| "Not for me" | Negative | −0.05 per category |
| Future: "I went" | Strong positive | +0.10 per category |

Stated preferences → hard constraints. Revealed → soft adjustments within constraints.
Gap exceeds threshold → surface tension to user in Gradio; never silently override.

### Personalization Strategies (priority order)
1. Category weight decay — `e^(-λt)`, half-life ~30 days
2. Chroma embedding similarity — cosine similarity to liked events
3. Implicit non-negative signal — unflagged exposure → small upward pressure
4. Periodic LLM profile revision (v2) — user approves diff in Gradio
5. Wildcard slot — 1–2 moderate-fit, high-novelty slots per week

---

## Description Quality Scoring

Deterministic weighted rubric. No LLM in v1.

```python
# scene_scout/config.py
DESCRIPTION_QUALITY_THRESHOLD: float = 0.3  # low_information = score < this
```

| Signal | Weight | Measurement |
|---|---|---|
| Description length | 0.20 | 0 chars → 0.0 / <50 → 0.3 / 50–150 → 0.7 / 150+ → 1.0 |
| Venue presence | 0.20 | Non-null, non-generic ("TBD", "Various") → 1.0; else 0.0 |
| Date + time present | 0.20 | Both → 1.0 / date only → 0.5 / neither → 0.0 |
| Performer/artist named | 0.15 | Named performer in title or description → 1.0; else 0.0 |
| Category coverage | 0.10 | ≥1 non-generic category → 1.0; else 0.0 |
| URL validity | 0.10 | Present and well-formed → 1.0; else 0.0 |
| Price clarity | 0.05 | `price_cents` set OR `is_free=True` → 1.0; else 0.0 |

`low_information = score < DESCRIPTION_QUALITY_THRESHOLD`

Applied as a confidence discount in Ranking, not a hard filter.

---

## Neighborhood Scout: Hyper-Local Architecture

Operates within a strict **15–20 minute walking radius** (~1 km) from the venue.
LLM recall cannot reliably reason about walking distances. Spatial data is required.

### Two-Mode Architecture

**Mode A — Geocoding-assisted (primary):**
1. Geocode venue via Nominatim → `(lat, lon)`
2. Query Nominatim for POIs within ~1 km: bars, restaurants, cafés, venues, galleries
3. Pass POI list as structured context to the LLM
4. LLM narrates and curates — it writes about facts it is given, not facts it recalls

**Mode B — LLM-only fallback:**
When geocoding fails or venue is unknown → general neighborhood character note only.
No specific business recommendations. Honest and safe.

### Geocoding Cache
- **Key:** `venue_name + city` (normalized)
- **Value:** `(lat, lon)` + POI list JSON
- **TTL:** 90 days

### Neighborhood Context Cache
- **Key:** `venue_name + city` (normalized)
- **Value:** context string + confidence float
- **TTL:** 30 days

Nominatim rate limit: 1 request/second. Cache aggressively. Not a constraint for a
weekly pipeline.

---

## Vibe Classifier Vocabulary

Fixed. 2–5 tags per event. No tags outside this list.

```
intimate        high-energy     experimental    social          introspective
outdoor         late-night      family-friendly industry        touristy
immersive       underground     high-production free-spirited   niche
single-friendly pretentious     exclusive       inclusive
```

`single-friendly` — comfortable solo; easy to meet people
`pretentious` — artistically serious; self-consciously avant-garde (not pejorative)
`exclusive` — curated entry, VIP feel, high barrier
`inclusive` — explicitly welcoming, diverse, low pressure

Quality check: any tag exceeding 40% of events in a run → classifier collapse. Investigate.

---

## Ranking Agent: Design Rationale

**Deterministic scoring** for score computation. **LLM generation** for explanations.
These are intentionally separate responsibilities.

Score computation must be reproducible: same inputs → same outputs, always. Stochastic
scoring makes the system impossible to debug or evaluate systematically.

Explanation generation is a natural language task where some variation is acceptable.
The LLM receives the score breakdown and event fields; it narrates the reasons. It does
not invent reasons.

**v2 path — LLM-as-judge reranking (Milestone 10):**
After deterministic scoring, pass top 20 candidates to an LLM with the user profile.
LLM produces final top 10 with reasoning. Editorial layer *on top of* scores, not
instead of them.

**Score components:**
- `category_fit` — overlap with `UserProfile.category_weights`
- `vibe_fit` — overlap with `UserProfile.vibe_preferences`
- `semantic_similarity` — cosine similarity to Chroma liked-events (0.0 cold start)
- `performer_affinity` — `top_performer_affinity` from Talent Scout
- `location` — proximity to user's preferred neighborhoods
- `novelty` — penalty for previously recommended; bonus for unseen categories/vibes
- `source_quality` — from feed config
- `description_quality` — confidence discount for `low_information` records

All components normalized to 0.0–1.0. Composite is a weighted sum. Weights are named
constants in `config.py`.

---

## Testing Strategy

| Test Type | Scope | Fixture Approach | Runs In |
|---|---|---|---|
| Unit tests | Agent logic, deterministic components, services | Mocked LLM calls (`services/llm.py` mocked) | CI on every commit |
| Integration tests | Agent-to-agent data flow; schema contracts | Mocked LLM; real SQLite/Chroma | CI on every commit |
| Prompt regression tests | LLM output quality; schema validity | Golden file fixtures (real API responses) | Manually; weekly |
| UAT | Full end-to-end pipeline; real email sent | Live API calls | Manually before each milestone |

**Golden file fixtures:** A set of stored real LLM responses as JSON files in
`tests/fixtures/golden/`. Prompt regression tests run against these. Regenerate
periodically against the live API to catch prompt drift.

---

## Gradio UI

Built-in Gradio auth enabled from day one:
```python
gr.Blocks(auth=("username", os.getenv("GRADIO_PASSWORD")))
```
Password stored in Modal Secrets as `GRADIO_PASSWORD`.

**User-facing sections:**
- Onboarding (name, email, cold-start prompt)
- Profile viewer and editor
- Feedback history
- Feed management (add, validate, disable user feeds)

**Dev Section (password-protected):**
- Pipeline run log viewer (filter by `run_id`, agent, level)
- Feed health dashboard
- Global feed management
- Dry-run trigger and email preview
- Recommendation history browser
- Cache inspection (hit rates, TTL status)

---

## Agent Roster

### 1. Feed Scout Agent
| | |
|---|---|
| Inputs | `list[FeedConfig]`, `run_id: str` |
| Outputs | `tuple[list[RawFeedEntry], list[FeedHealthReport]]` |
| LLM | No |
| Deterministic | Yes |
| Log color | Cyan |
| Failure handling | Per-feed: log + skip. Never halts pipeline. |

---

### 2. Event Extraction Agent
| | |
|---|---|
| Inputs | `list[RawFeedEntry]`, `run_id: str` |
| Outputs | `list[EventCandidate]` |
| LLM | Yes — via `llm.complete()` |
| Deterministic | Schema validation + `is_event` filter |
| Log color | Blue |
| Failure handling | Per-entry: log + skip on `LLMValidationError` |

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
| Deterministic | Yes — date parsing, URL validation, category standardization |
| Log color | Blue |
| Failure handling | Per-record: unparseable date → log + discard |

```python
class NormalizedEvent(BaseModel):
    id: str                        # SHA-256: title + date + venue
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
    source_feed: str
    source_quality_score: float
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
| LLM | Optional escalation via `llm.complete()` |
| Deterministic | Yes for exact/fuzzy; probabilistic for escalation |
| Log color | Blue |
| Escalation | exact ID → fuzzy (`rapidfuzz`) → embedding → LLM |

---

### 5. Description Quality Agent
| | |
|---|---|
| Inputs | `list[NormalizedEvent]`, `run_id: str` |
| Outputs | `list[NormalizedEvent]` with `description_quality_score`, `low_information` |
| LLM | No (v1) |
| Deterministic | Yes |
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
| Failure handling | Per-event: validation error → empty `performers` list; log warning |
| Cache | Performer name → `PerformerInfo`; TTL 90 days |

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
| Failure handling | Per-event: validation error → empty `vibe_tags`; log warning |
| Cache | Content hash → tag list; TTL 14 days |

---

### 8. Neighborhood Scout Agent
| | |
|---|---|
| Inputs | `list[EnrichedEvent]`, `UserProfile`, `run_id: str` |
| Outputs | `list[EnrichedEvent]` with `neighborhood_context` |
| LLM | Yes — narrates geocoded POI data; via batch strategy |
| Log color | Magenta |
| Failure handling | Per-event: geocoding fail or low confidence → `None`; log warning |
| Cache | Venue + city → context + confidence; TTL 30 days |
| Mode A | Nominatim geocoding → POI list → LLM narration |
| Mode B | Fallback: general neighborhood note only; no specific businesses |

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
    name: str                          # From onboarding
    email: str                         # Copy; source of truth is Modal Secret
    stated_interests: list[str]
    stated_dislikes: list[str]
    preferred_neighborhoods: list[str]
    max_travel_minutes: Optional[int]
    budget_ceiling_cents: Optional[int]
    excluded_categories: list[str]     # Hard constraints
    category_weights: dict[str, float] # Soft weights; decay-updated
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
| LLM | Yes — explanations only via `llm.complete()` |
| Deterministic | Yes — scoring is reproducible |
| Log color | Green |
| Failure handling | LLM explanation failure → generic fallback explanation; log warning |

```python
class RankedEvent(BaseModel):
    event: EnrichedEvent
    score: float
    score_breakdown: dict[str, float]
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
| Deterministic | Yes — diversity rules, history filtering |
| Log color | Gold |
| Sub-10 | Sends what passed; Allegra's note explains plainly |

**Diversity rules (v1):**
- Max 3 events per top-level category
- Max 2 events per venue
- At least 2 different dates
- 1–2 wildcard slots (moderate fit, high novelty)
- Last 4 weeks: score × 0.5; last 2 weeks: hard exclude

```python
class CuratedRecommendation(BaseModel):
    rank: int
    event: EnrichedEvent
    score: float
    score_breakdown: dict[str, float]
    explanation: str
    neighborhood_context: Optional[str]
    sellout_risk: str                    # "low"|"medium"|"high"
    sellout_urgency_note: Optional[str]
    feedback_token: str                  # UUID
    is_wildcard: bool
    run_id: str
    recommended_at: datetime
```

---

### 13. Email Composer Agent
| | |
|---|---|
| Inputs | `list[CuratedRecommendation]`, `UserProfile`, `run_id: str` |
| Outputs | Rendered HTML; Resend send confirmation |
| LLM | Yes — via `llm.complete()` |
| Log color | Gold |
| Email address | Read from `USER_EMAIL` Modal Secret |
| UAT | Subject: `[UAT {run_id}] Your picks this week` |
| Failure handling | Resend failure → `LLMInfrastructureError`; pipeline halts |

---

### 14. Evaluation Agent
| | |
|---|---|
| Inputs | Run logs, recommendations, feedback, `UserProfile`, `run_id` |
| Outputs | Quality report; flagged issues |
| LLM | Yes — smaller model via `llm.complete()` |
| Log color | Red |

---

## Persistent Stores

| Store | Volume | Format | Owner | Retention |
|---|---|---|---|---|
| Global feeds | repo | `config/global_feeds.yaml` | Operator | Permanent |
| User feeds | repo | `config/user_feeds.yaml` | User | Permanent |
| User profile | `vol-profiles` | JSON | User Preference Agent | Permanent |
| Chroma index | `vol-chroma` | Chroma DB | Ranking Agent | Permanent |
| Feedback events | `vol-feedback` | SQLite | Feedback Service | Permanent |
| Recommendation history | `vol-history` | SQLite | Curator Agent | Permanent |
| Enrichment + geocoding cache | `vol-cache` | SQLite | Enrichment agents | TTL-based |
| Pipeline state | `vol-pipeline-state` | JSON | Orchestrator | Cleared post-run |
| Run logs | `vol-logs` | JSONL | All agents | 90 days rolling |

---

## Repository Structure

```
scene-scout/
├── docker/
│   ├── pipeline/Dockerfile
│   └── web/Dockerfile
├── docker-compose.yml
├── pyproject.toml
├── uv.lock
├── .env.example
├── config/
│   ├── global_feeds.yaml
│   └── user_feeds.yaml
├── docs/
│   ├── architecture.md
│   ├── project_plan.md
│   └── diagrams/
│       ├── diagrams.md
│       ├── system_architecture.mmd
│       └── data_flow.mmd
├── output/                           ← UAT output; gitignored
├── scene_scout/
│   ├── __init__.py
│   ├── cli.py                        ← UAT entry point
│   ├── orchestrator.py               ← run_id; PipelineState; agent sequencing
│   ├── config.py                     ← all named constants and env vars
│   ├── curator_config.py             ← CuratorConfig; Allegra
│   ├── agents/
│   │   ├── feed_scout.py
│   │   ├── event_extraction.py
│   │   ├── event_normalization.py
│   │   ├── deduplication.py
│   │   ├── description_quality.py
│   │   ├── talent_scout.py
│   │   ├── vibe_classifier.py
│   │   ├── neighborhood_scout.py
│   │   ├── user_preference.py
│   │   ├── ranking.py
│   │   ├── sellout_risk.py
│   │   ├── recommendation_curator.py
│   │   ├── email_composer.py
│   │   └── evaluation.py
│   ├── models/
│   │   ├── feed.py
│   │   ├── event.py
│   │   ├── enrichment.py
│   │   ├── ranking.py
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
│   │   ├── batch.py                  ← BatchStrategy + implementations
│   │   ├── cache.py                  ← enrichment + geocoding cache
│   │   ├── chroma.py                 ← Chroma client + embedding
│   │   ├── feedback.py               ← feedback signal store
│   │   ├── geocoding.py              ← Nominatim wrapper + cache
│   │   ├── history.py                ← recommendation history store
│   │   ├── llm.py                    ← centralized LLM service
│   │   └── prompt_loader.py          ← Jinja2 render_prompt()
│   └── logging/
│       └── logger.py                 ← rich logger; color-coded per agent
└── tests/
    ├── fixtures/
    │   └── golden/                   ← golden file fixtures for prompt regression
    ├── agents/
    ├── models/
    └── services/
```

---

## Design Principles

1. Separate the scaffold from the intelligence — interface and implementation are independent.
2. Build the skeleton before the intelligence — pipeline first, smart agents second.
3. LiteLLM is the only LLM interface — no direct provider SDK imports in agents.
4. All LLM calls go through `services/llm.py` — one entry point, consistent behavior.
5. Prompts are Jinja2 templates in files — versioned independently of agent code.
6. Degrade gracefully at the record level; fail fast at the infrastructure level.
7. Agents communicate via direct return values — no shared mutable state except `PipelineState` at the batch boundary.
8. Neighborhood Scout narrates facts it is given — it does not recall geography.
9. Scoring is deterministic; explanation is generative — never conflate them.
10. UAT sends a real email — if it didn't arrive, the test didn't pass.
11. Log with intent — color-coded, structured, traceable by `run_id`.
12. NumPy-style docstrings on all public functions and classes.
13. Add tests where they protect core behavior; golden files for prompt regression.
14. Every major component must answer: "Is this working?"
