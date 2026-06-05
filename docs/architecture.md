# SceneScout: System Architecture

## Overview

SceneScout is a personalized event discovery agent that reads local RSS feeds, extracts and
normalizes event data, ranks events against a user's taste profile, and delivers a curated
weekly top 10 recommendation email. The system is designed as a multi-agent application,
where each agent owns a discrete responsibility with defined inputs, outputs, and failure modes.

SceneScout is also a learning project. Every architectural decision should be explainable.
Prefer clarity over cleverness, deterministic logic over unnecessary LLM calls, and structured
data over prose wherever possible.

---

## Technology Stack

| Layer | Technology | Rationale |
|---|---|---|
| UI | Gradio | Fast, Python-native UI for cold-start prompt, profile review, dry-run output, feed management, and dev logs. Not a production web framework — keep it thin. |
| Vector store | Chroma | Stores embeddings of past events for semantic similarity ranking. Local-first, Modal-compatible via persistent volume. |
| Embeddings | `sentence-transformers` (`all-MiniLM-L6-v2`) | Open-source, fast, good enough for event-interest similarity. Avoids embedding API costs entirely. |
| Extraction LLM | Claude (Anthropic API) | Structured extraction from messy RSS prose requires a capable model. This is not a place to cut costs. |
| Ranking / explanation LLM | Claude (Anthropic API) | Explanation quality directly affects feedback quality. Keep capable. |
| Evaluation LLM | Smaller model acceptable | Lower stakes; LLM-as-judge for internal review only. |
| Deployment | Modal | Handles scheduling, compute isolation, secret management, Gradio serving, and persistent volumes. |
| Async HTTP | `httpx` (async) | All RSS feeds fetched concurrently via `asyncio.gather()`. |
| Schema validation | Pydantic v2 | All agent inputs and outputs are typed and validated. |
| Persistent stores | SQLite (feedback, history) + JSON (user profile) + YAML (feed config) | Right tool per store. See Persistent Stores section. |

### Open-Source Cost Mitigation Strategy

Use open-source where the task is well-defined and failure is cheap. Keep a capable hosted
model where output is user-facing or feeds downstream agents.

| Component | Approach |
|---|---|
| Embeddings | `sentence-transformers` local model — zero API cost |
| Feed parsing | `feedparser` — deterministic, no LLM |
| Date parsing | `dateutil` — deterministic, no LLM |
| Fuzzy matching (deduplication) | `rapidfuzz` — no LLM for baseline |
| Extraction + ranking + email | Claude API — quality matters here |
| Evaluation agent | Smaller/cheaper model acceptable |

---

## Deployment: Modal

### Scheduled Pipeline
The weekly pipeline runs as a Modal scheduled function. Every execution creates a new
`run_id` and processes the full agent sequence end-to-end.

### Gradio UI
Served as a Modal web endpoint. Thin: cold-start prompt input, profile review, dry-run
preview, feed management, feedback history, and dev log viewer.

### Secret Management
All credentials live in Modal Secrets. Never in `.env` files in production. Naming convention:

```
ANTHROPIC_API_KEY     → Modal secret: anthropic
EMAIL_API_KEY         → Modal secret: email
```

### Persistent Volumes (one per store — decoupled by design)

| Volume | Contents | Owner |
|---|---|---|
| `vol-chroma` | Chroma vector index (liked event embeddings) | Ranking Agent |
| `vol-feedback` | SQLite feedback event log | Feedback Service |
| `vol-history` | SQLite recommendation history | Recommendation Curator |
| `vol-profiles` | JSON user profile files | User Preference Agent |
| `vol-logs` | Structured pipeline run logs | Orchestrator + all agents |

Separate volumes mean each store can be wiped, backed up, or inspected independently
without affecting others.

---

## Run Identity

Every pipeline execution is assigned a `run_id` at orchestrator startup:

```python
run_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
# Example: "20250606-143022"
```

The `run_id`:
- Is generated **once** by the orchestrator, not by any agent
- Is passed **explicitly** as a parameter to every agent — never as a global
- Is attached to every log entry, every `RawFeedEntry`, every `CuratedRecommendation`,
  and every `FeedbackEvent`
- Is stored in recommendation history so feedback signals can be correlated back to
  the exact pipeline run that produced them
- Maps directly to a log file: `logs/20250606-143022.log`

Human-readable format sorts lexicographically and maps immediately to a wall-clock time,
making log correlation and debugging fast.

---

## Personalization Model

SceneScout uses a **two-phase personalization model**.

### Phase 1 — Cold Start (Stated Preferences)

On first use, the user submits a free-text prompt via the Gradio UI:

> "I love experimental music, independent film, and weird art shows. I hate corporate
> events and anything in a stadium. I'm in Silver Lake and don't want to travel more
> than 20 minutes."

The **User Preference Agent** parses this into a structured `UserProfile`. This profile
bootstraps the ranker from the first run.

### Phase 2 — Warm Personalization (Revealed Preferences)

Over time, behavioral signals update the preference profile:

| Signal | Type | Category weight delta |
|---|---|---|
| Link click (event URL) | Weak positive | +0.03 per category |
| "Not for me" click | Negative | -0.05 per category |
| Future: "I went" | Strong positive | +0.10 per category |

**Key design tension:** Stated and revealed preferences can contradict. Resolution strategy:

- Stated preferences act as **hard constraints** (location, budget, excluded categories)
- Revealed preferences act as **soft scoring adjustments** within those constraints
- When the gap exceeds a threshold, the system surfaces the tension to the user via
  Gradio — it does not silently override stated intent

### Personalization Improvement Strategies

Applied in priority order:

1. **Category weight decay** — older signals matter less. Apply exponential decay with
   a ~30-day half-life: `weight *= e^(-λt)`. Profile stays current with who the user
   is now, not who they were at signup.

2. **Chroma embedding similarity** — embed each positively-engaged event using
   `sentence-transformers`. Store in the `vol-chroma` "liked events" collection. At
   ranking time, score new events by cosine similarity to this collection. Captures
   taste at finer grain than category labels allow.

3. **Implicit non-negative signal** — events never flagged across many exposures
   represent revealed tolerance. Apply a small upward pressure on those category weights.
   Do not over-index: absence of complaint is not enthusiasm.

4. **Periodic LLM profile revision** (v2) — every N weeks, pass the full feedback
   history and current profile to the User Preference Agent. LLM produces a revised
   profile with reasoning. User reviews the diff in Gradio before it is applied.

5. **Wildcard slot** — reserve 1–2 slots in the top 10 for events that score moderately
   on fit but highly on novelty: unseen categories, new venues, different price ranges.
   Track whether wildcard events get clicked or flagged — that is the highest-value
   signal for profile expansion.

---

## Core Learning Loop

```
User submits cold-start prompt (Gradio)
        ↓
User Preference Agent → Structured UserProfile
        ↓
[Weekly Pipeline — orchestrated by Modal scheduler]
        ↓
Feed Scout → Event Extraction → Normalization → Deduplication
        ↓
Ranking Agent (uses UserProfile + Chroma similarity)
        ↓
Sell-Out Risk Agent
        ↓
Recommendation Curator → top 10 with feedback tokens
        ↓
Email Composer → sends email with explanations + feedback links
        ↓
User clicks event link → tracked redirect → weak positive signal logged
User clicks "Not for me" → negative signal logged
        ↓
Feedback Service → FeedbackEvent stored in vol-feedback
        ↓
User Preference Agent → reads signals → applies decay-weighted delta update
        ↓
Next run uses updated UserProfile + updated Chroma index
```

Three features drive this loop:

1. **Explanation per Recommendation** — makes reasoning visible; enables better feedback
2. **Feedback Button + Link Click Tracking** — collects revealed preference signals
3. **Previously Recommended Memory** — prevents repetition; anchors the learning dataset

---

## Link Click Tracking

Event links in the email do not point directly to the event URL. They route through a
Modal tracking endpoint:

```
https://scenescout.modal.app/track?token={feedback_token}&signal=click&redirect={event_url}
```

The endpoint logs the signal and immediately redirects. From the user's perspective:
a normal link. From the system's perspective: a behavioral signal with full context
(run_id, event_id, rank position, score breakdown at time of recommendation).

The same `feedback_token` infrastructure handles both negative signals ("Not for me")
and positive signals (clicks) through one endpoint with two signal types.

---

## Sub-10 Candidate Handling

If fewer than 10 events pass the full pipeline in a given week, the Curator does not
pad with low-quality candidates or silently send a short list. Instead, the email
includes a note from **the Curator** (who has a name, chosen before Milestone 5)
explaining that they did not find 10 events worthy of the user's time this week, and
listing what was found. Honesty over padding.

The Curator's name and voice must be consistent across: the email intro, the sub-10
message, and any future user-facing copy. Define the character before writing the
Email Composer prompt.

---

## RSS Feed Management

Feeds are managed at two levels:

### Global Feeds (`config/global_feeds.yaml`)
Operator-curated feeds available to all users. Managed via the Gradio Dev Section.

### User Feeds (`config/user_feeds.yaml` or per-user SQLite rows)
User-added feeds. Added via the Gradio UI. Defaulted to `source_quality_score: 0.5`
until the system has seen enough entries from them to calibrate.

### Feed Validation on Add
When a user submits a new RSS URL, the system runs a lightweight `validate_feed()`
call synchronously before saving:
- Fetches the feed
- Parses it
- Checks for at least one entry
- Returns a health report shown in the UI

This prevents dead or malformed feeds from silently entering the pipeline.

### Feed Scout merges both lists at runtime, deduplicates by URL, and processes all
active feeds concurrently via `asyncio.gather()`.

---

## Gradio UI — Defined Scope

Gradio is not a general-purpose frontend. Its scope is fixed:

**User-facing sections:**
- Cold-start prompt submission and profile review
- Feedback history viewer (what was recommended, what was flagged)
- Feed management (add, disable, remove user feeds)
- Preference profile editor (view and manually adjust weights)

**Dev Section (operator-only):**
- Pipeline run log viewer (searchable by `run_id`)
- Feed health dashboard (last fetch status per feed)
- Global feed management
- Dry-run trigger and output preview
- Recommendation history browser

The Dev Section is separated visually and, in production, access-controlled.

---

## Chroma — What It Stores and What It Does Not

**Chroma stores:** Embeddings of events the user has positively engaged with (clicked,
liked, or not flagged across many exposures). Used by the Ranking Agent for semantic
similarity scoring at ranking time.

**Chroma does not store:** User profiles, feedback events, or recommendation history.
Those are structured records that belong in SQLite and JSON, not a vector store.
Putting structured data in a vector store because it is available is a category error.

**Embedding model:** `sentence-transformers/all-MiniLM-L6-v2`. Fast, local, no API cost.
What is embedded: event `title + description + categories` as a single text block.

---

## Agent Roster

### 1. Feed Scout Agent

**Responsibility:** Fetch, parse, validate, and monitor RSS feed sources concurrently.

| Field | Detail |
|---|---|
| Inputs | `list[FeedConfig]`; `run_id` |
| Outputs | `tuple[list[RawFeedEntry], list[FeedHealthReport]]` |
| Tools | `httpx` async HTTP; `feedparser` |
| LLM Required? | No |
| Deterministic Logic Required? | Yes — reliability is the entire job |
| Failure Modes | Feed unreachable; feed malformed; feed stale; feed returns no entries |
| Validation | Entry count per feed; recency check; schema presence |
| Evaluation | Feed uptime rate; entry yield per feed; malformed rate |
| Why an agent? | Owns feed health monitoring and concurrent fetch orchestration across multiple sources |

**Implementation note:** All feeds fetched concurrently via `asyncio.gather()`. One feed
failure never affects others. `run_id` attached to every `RawFeedEntry`.

---

### 2. Event Extraction Agent

**Responsibility:** Convert raw RSS entries into structured event candidates using an LLM.

| Field | Detail |
|---|---|
| Inputs | `list[RawFeedEntry]`; `run_id` |
| Outputs | `list[EventCandidate]` |
| Tools | Claude API with structured output |
| LLM Required? | Yes — RSS prose is inconsistently formatted |
| Deterministic Logic Required? | Yes — schema validation, fallback handling, `is_event` filtering |
| Failure Modes | Entry is not an event; required fields missing; date not extractable; hallucinated venue |
| Validation | Pydantic schema enforcement; `is_event=False` entries discarded; date plausibility check |
| Evaluation | Field extraction accuracy vs. human-labeled sample; hallucination rate; failure rate per source |
| Why an agent? | Owns the judgment of whether an entry is an event and what its structured fields are |

```python
class EventCandidate(BaseModel):
    title: str
    date: Optional[str]           # Raw string — normalized downstream
    time: Optional[str]
    venue: Optional[str]
    neighborhood: Optional[str]
    city: str
    url: str
    price: Optional[str]
    description: Optional[str]
    categories: list[str]
    is_event: bool                # LLM judgment
    extraction_confidence: float  # 0.0–1.0
    source_feed: str
    run_id: str
    extracted_at: datetime
```

---

### 3. Event Normalization Agent

**Responsibility:** Clean and standardize extracted event records for ranking.

| Field | Detail |
|---|---|
| Inputs | `list[EventCandidate]`; `run_id` |
| Outputs | `list[NormalizedEvent]` |
| Tools | `dateutil` for date parsing; deterministic URL validation; optional LLM for ambiguous location |
| LLM Required? | Sparingly — only where deterministic logic cannot resolve |
| Deterministic Logic Required? | Yes — date parsing, URL validation, category standardization |
| Failure Modes | Unparseable date; missing venue; event not in coming week; URL 404 |
| Validation | Date is within the coming week; URL reachable; required fields present |
| Evaluation | Normalization success rate; date parse failure rate; invalid URL rate |
| Why an agent? | Owns transformation from raw extraction to ranking-ready records |

```python
class NormalizedEvent(BaseModel):
    id: str                        # Stable hash: title + date + venue
    title: str
    start_datetime: datetime
    end_datetime: Optional[datetime]
    venue: str
    neighborhood: Optional[str]
    city: str
    url: str
    price_cents: Optional[int]     # Normalized to cents; None = unknown
    is_free: bool
    description: str
    categories: list[str]          # Standardized controlled vocabulary
    source_feed: str
    source_quality_score: float
    run_id: str
    normalized_at: datetime
```

---

### 4. Deduplication Agent

**Responsibility:** Identify and collapse duplicate or near-duplicate events across feeds.

| Field | Detail |
|---|---|
| Inputs | `list[NormalizedEvent]`; `run_id` |
| Outputs | Deduplicated `list[NormalizedEvent]`; merge log |
| Tools | Exact match on `event.id`; `rapidfuzz` for fuzzy title+venue+date matching; Chroma similarity as escalation |
| LLM Required? | Optionally for ambiguous near-duplicates |
| Deterministic Logic Required? | Yes — exact deduplication must be deterministic |
| Failure Modes | False positive merge; false negative (duplicate survives) |
| Validation | Merge log review; duplicate rate per source pair |
| Evaluation | Precision and recall against human-labeled duplicate pairs |
| Why an agent? | Owns the merge decision: which record to keep, what metadata to preserve |

**Escalation strategy:** Exact ID match → fuzzy match (rapidfuzz) → embedding similarity
(Chroma) → LLM judgment. Stop at the first level that produces a confident decision.

---

### 5. User Preference Agent

**Responsibility:** Parse the cold-start prompt and maintain the evolving taste profile.

| Field | Detail |
|---|---|
| Inputs | Cold-start prompt (first run); feedback signals from `vol-feedback` (subsequent runs); `run_id` |
| Outputs | `UserProfile` written to `vol-profiles` |
| Tools | Claude API for prompt parsing and periodic revision; deterministic delta logic for feedback application |
| LLM Required? | Yes — for initial parsing and periodic profile revision |
| Deterministic Logic Required? | Yes — for applying feedback deltas with exponential decay |
| Failure Modes | Prompt misinterpretation; contradictory signals; profile drift from stated intent |
| Validation | Schema validation; Gradio profile review on first run |
| Evaluation | Ranking alignment with stated preferences (early); revealed preferences (later) |
| Why an agent? | Owns interpretation of user intent and resolution of stated vs. revealed preference tensions |

```python
class UserProfile(BaseModel):
    user_id: str
    email: str
    stated_interests: list[str]
    stated_dislikes: list[str]
    preferred_neighborhoods: list[str]
    max_travel_minutes: Optional[int]
    budget_ceiling_cents: Optional[int]
    excluded_categories: list[str]     # Hard constraints — never overridden by feedback
    category_weights: dict[str, float] # Soft weights — updated by decayed feedback signals
    created_at: datetime
    last_updated: datetime
    profile_version: int
```

**v1 update logic:** On each feedback signal, apply a decay-weighted delta to
`category_weights` for all categories of the flagged event. Decay factor:
`e^(-λt)` where `t` = signal age in days, half-life ≈ 30 days.

**v2 update logic (future):** Periodic LLM revision. Pass current profile + full
feedback history. LLM produces revised profile with reasoning. User approves diff
in Gradio before it is applied.

---

### 6. Ranking Agent

**Responsibility:** Score candidate events against the user profile and produce ranked
events with explanations.

| Field | Detail |
|---|---|
| Inputs | Deduplicated `list[NormalizedEvent]`; `UserProfile`; Chroma liked-events index; `run_id` |
| Outputs | `list[RankedEvent]` with scores, breakdowns, and explanations |
| Tools | Deterministic scoring logic; `sentence-transformers` + Chroma for semantic similarity; Claude API for explanation generation |
| LLM Required? | Yes — for explanation strings |
| Deterministic Logic Required? | Yes — score computation must be reproducible given same inputs |
| Failure Modes | Generic explanations; score compression (everything scores similarly); Chroma index empty on first run |
| Validation | Score distribution check; explanation grounded in actual score components |
| Evaluation | Ranking vs. user feedback; explanation relevance; score stability across runs |
| Why an agent? | Owns scoring judgment and per-event explanation grounded in user profile |

```python
class RankedEvent(BaseModel):
    event: NormalizedEvent
    score: float                       # 0.0–1.0 composite
    score_breakdown: dict[str, float]  # e.g. {"category_fit": 0.8, "semantic_similarity": 0.7, "location": 0.9, "novelty": 0.6, "source_quality": 0.8}
    explanation: str                   # Grounded in score_breakdown — not generic
    is_previously_recommended: bool
    novelty_penalty_applied: bool
    wildcard_slot: bool                # True if this event fills a diversity/exploration slot
    run_id: str
```

**Score components:**
- `category_fit`: overlap between event categories and `UserProfile.category_weights`
- `semantic_similarity`: cosine similarity to liked-events Chroma collection (0.0 on cold start)
- `location`: proximity to preferred neighborhoods
- `novelty`: penalty for previously recommended events; bonus for unseen categories
- `source_quality`: inherited `source_quality_score` from feed config

**Explanation constraint:** The LLM prompt receives event fields, score breakdown, and
user interests. It must produce an explanation that cites specific reasons from the data.
"This matches your interests" is a quality failure.

---

### 7. Sell-Out Risk Agent

**Responsibility:** Estimate whether an event may sell out and require advance purchase.

| Field | Detail |
|---|---|
| Inputs | `list[RankedEvent]`; `run_id` |
| Outputs | `list[RankedEvent]` with `sellout_risk` populated |
| Tools | Heuristic classifier (v1); trained classifier (future) |
| LLM Required? | Optional for v1 |
| Deterministic Logic Required? | Yes — rule-based signals |
| Failure Modes | Overconfident scores; poor calibration for niche event types |
| Validation | Risk score distribution; manual spot-check |
| Evaluation | Precision/recall against historical sell-out events (once data exists) |
| Why an agent? | Owns the urgency classification signal surfaced to the user |

**v1 heuristic signals:** venue size category; price; day-of-week; proximity to event
date; description language ("limited capacity", "tickets going fast"); source quality.

---

### 8. Recommendation Curator Agent

**Responsibility:** Select the final top 10 and compose the curated list with diversity
controls and recommendation history awareness.

| Field | Detail |
|---|---|
| Inputs | `list[RankedEvent]`; `UserProfile`; recommendation history from `vol-history`; `run_id` |
| Outputs | `list[CuratedRecommendation]` (up to 10) |
| Tools | `vol-history` SQLite; deterministic diversity rules |
| LLM Required? | Optional — for edge case resolution |
| Deterministic Logic Required? | Yes — diversity rules, history filtering, slot filling |
| Failure Modes | All slots same category; previously recommended events resurface; fewer than 10 candidates |
| Validation | Category diversity check; history overlap check; slot count |
| Evaluation | Diversity score; repeat recommendation rate; engagement rate by rank position |
| Why an agent? | Owns final editorial judgment: the list must be useful, not merely sorted |

**The Curator has a name.** Choose before Milestone 5. The Curator's voice appears in:
the email intro, the sub-10 message, and all future user-facing copy. Define the
character brief before writing the Email Composer prompt.

**Sub-10 behavior:** If fewer than 10 events pass the pipeline, the Curator sends what
it has with a note explaining that it did not find 10 events worthy of the user's time
this week. No padding with low-quality candidates.

**Diversity rules (v1):**
- Max 3 events per top-level category
- Max 2 events per venue
- At least 2 different dates in the coming week
- 1–2 wildcard slots: moderate fit, high novelty
- Recency penalty: events recommended in last 4 weeks → score × 0.5
- Hard exclude: events recommended in last 2 weeks (unless fewer than 10 candidates)

```python
class CuratedRecommendation(BaseModel):
    rank: int                            # 1–10
    event: NormalizedEvent
    score: float
    score_breakdown: dict[str, float]
    explanation: str
    sellout_risk: str                    # "low" | "medium" | "high"
    sellout_urgency_note: Optional[str]
    feedback_token: str                  # UUID — used in both "Not for me" and click-tracking links
    is_wildcard: bool
    run_id: str
    recommended_at: datetime
```

---

### 9. Email Composer Agent

**Responsibility:** Write and send the weekly recommendation email.

| Field | Detail |
|---|---|
| Inputs | `list[CuratedRecommendation]`; `UserProfile`; `run_id` |
| Outputs | Rendered HTML email; send confirmation |
| Tools | Email delivery API (Resend or SendGrid) |
| LLM Required? | Yes — for intro paragraph and event blurbs |
| Deterministic Logic Required? | Yes — template structure, link generation, dry-run mode |
| Failure Modes | Email unsent; tracking links broken; hallucinated event details |
| Validation | All events present; links valid; no hallucinated fields (LLM uses only provided data) |
| Evaluation | Feedback click rate; link click rate |
| Why an agent? | Owns user-facing content generation and delivery |

**Email structure:**
1. Personalized intro from the Curator (1–2 sentences, LLM-generated, Curator voice)
2. For each recommendation (1–10):
   - Title, date, time, venue, neighborhood, price
   - 2–3 sentence description (grounded in event data — no hallucination)
   - **Why this was picked for you** (explanation from Ranking Agent, passed through intact)
   - Sell-out urgency note if applicable
   - Event link: `https://scenescout.modal.app/track?token={token}&signal=click&redirect={event_url}`
   - "Not for me →" link: `https://scenescout.modal.app/feedback?token={token}&signal=negative`
3. Footer: profile update link (Gradio URL)

**Dry-run mode:** `DRY_RUN=true` → full pipeline runs, composed email written to
`vol-logs/{run_id}/email_preview.html`, nothing sent.

---

### 10. Evaluation Agent

**Responsibility:** Review system output quality and support continuous improvement.

| Field | Detail |
|---|---|
| Inputs | Pipeline run logs; `list[CuratedRecommendation]`; feedback signals; `UserProfile`; `run_id` |
| Outputs | Quality report; flagged issues; suggested profile adjustments |
| Tools | Log reader; LLM-as-judge (smaller model acceptable) |
| LLM Required? | Yes — for qualitative assessment |
| Deterministic Logic Required? | Yes — schema checks, coverage checks, diversity metrics |
| Failure Modes | False positive flags; false negative misses |
| Validation | Human review of flagged issues via Gradio Dev Section |
| Evaluation | Issue detection rate; false positive rate; correlation with user feedback |
| Why an agent? | Owns meta-level judgment of whether the system is working well |

---

## The Three Learning Loop Features

### Feature 1: Explanation per Recommendation
**Owner:** Ranking Agent (generates); Email Composer Agent (surfaces unchanged)

Every recommendation includes a 1–2 sentence explanation grounded in the score breakdown
and user profile. The explanation is generated in the Ranking Agent — not the Email
Composer — and passed through intact. Generic explanations ("This matches your interests")
are a quality failure and should be flagged by the Evaluation Agent.

### Feature 2: Feedback Button + Link Click Tracking
**Owner:** Recommendation Curator (generates tokens); Modal tracking endpoint (receives);
Feedback Service (stores); User Preference Agent (reads and applies)

Every recommendation has one `feedback_token`. That token is used in both the "Not for me"
link and the click-tracking redirect. One endpoint, two signal types. Signals stored with
full context: `run_id`, `event_id`, `event_categories`, `score_breakdown`, `rank`,
`received_at`.

```python
class FeedbackEvent(BaseModel):
    token: str
    user_id: str
    event_id: str
    event_categories: list[str]
    score_breakdown: dict[str, float]
    rank: int
    signal: str                # "click" | "negative" | "positive"
    run_id: str
    received_at: datetime
```

### Feature 3: Previously Recommended Memory
**Owner:** Recommendation Curator Agent

Persistent log in `vol-history` SQLite. Every recommended event is written after sending.
On each run, candidates are scored against the log before finalizing the top 10. The history
entry is updated when a feedback signal arrives, linking recommendation to reaction — making
the store a labeled dataset, not just a log.

```python
class RecommendationHistoryEntry(BaseModel):
    event_id: str
    event_title: str
    run_id: str
    rank: int
    recommended_at: datetime
    feedback_received: bool
    feedback_signal: Optional[str]   # "click" | "negative" | "positive" — populated post-send
```

---

## Data Flow Summary

```
RSS Feeds (global + user-added, validated on add)
        ↓
Feed Scout Agent [async, concurrent] → RawFeedEntry[] + FeedHealthReport[]
        ↓
Event Extraction Agent [LLM, structured output] → EventCandidate[]
        ↓
Event Normalization Agent [deterministic + selective LLM] → NormalizedEvent[]
        ↓
Deduplication Agent [exact → fuzzy → embedding → LLM] → NormalizedEvent[]
        ↓
        ← UserProfile (vol-profiles)
        ← Chroma liked-events index (vol-chroma)
Ranking Agent [scoring + LLM explanations] → RankedEvent[]
        ↓
Sell-Out Risk Agent [heuristic classifier] → RankedEvent[]
        ↓
        ← Recommendation History (vol-history)
Recommendation Curator → CuratedRecommendation[] (≤10)
        ↓
Email Composer Agent [LLM + template] → HTML email with tracking links
        ↓
User receives email
  → clicks event link → Modal tracking endpoint → click signal → vol-feedback
  → clicks "Not for me" → Modal feedback endpoint → negative signal → vol-feedback
        ↓
User Preference Agent
  → applies decay-weighted delta to UserProfile
  → updates Chroma index with positively-engaged event embeddings
        ↓
Next run uses updated UserProfile + updated Chroma index
```

---

## Persistent Stores

| Store | Volume | Format | Owner | Purpose |
|---|---|---|---|---|
| Global feed config | repo | `config/global_feeds.yaml` | Operator / Feed Scout | Curated RSS sources |
| User feed config | repo or SQLite | `config/user_feeds.yaml` | User / Feed Scout | User-added RSS sources |
| User profiles | `vol-profiles` | JSON per user | User Preference Agent | Structured taste profile |
| Chroma index | `vol-chroma` | Chroma DB | Ranking Agent | Liked-event embeddings for semantic similarity |
| Feedback events | `vol-feedback` | SQLite | Feedback Service | Behavioral signal log |
| Recommendation history | `vol-history` | SQLite | Recommendation Curator | Recommended events + feedback linkage |
| Pipeline run logs | `vol-logs` | Structured log files | Orchestrator + agents | Observability, Dev Section viewer |

---

## Milestone Plan

### Milestone 1: Deterministic RSS Pipeline ✓
Async feed fetching, raw entry model, feed validation, health reporting, logging.

### Milestone 2: Event Extraction
LLM-based extraction to `EventCandidate`. Structured output. Schema validation. Failure handling.

### Milestone 3: Normalization and Deduplication
Date parsing, venue cleanup, category standardization, fuzzy deduplication baseline.

### Milestone 4: User Preference and Ranking
Cold-start prompt → `UserProfile`. Scoring pipeline. Explanation generation. `RankedEvent` output.
**Feedback token infrastructure and tracking endpoint begin here.**

### Milestone 5: Weekly Email Generation
Curator (named). Top 10 curation with diversity rules. Email composition. Tracking links.
Dry-run mode. Recommendation history store initialized.

### Milestone 6: Feedback Loop Activation
Modal tracking endpoint live. Signals stored in `vol-feedback`. User Preference Agent reads
and applies decay-weighted delta updates. Chroma index updated from positive signals.
Labeled dataset begins accumulating.

### Milestone 7: Sell-Out Risk Prediction
Heuristic classifier. Urgency notes in email. Future ML model design.

### Milestone 8: Evaluation, Observability, and Gradio Dev Section
Evaluation Agent. Structured run logs. Gradio Dev Section: log viewer, feed health dashboard,
dry-run trigger, recommendation history browser. Regression tests for ranking behavior.

---

## Design Principles

1. Build small, testable components.
2. Separate deterministic pipelines from LLM reasoning.
3. Use agents only where judgment, interpretation, planning, or critique is required.
4. Make data flow explicit and inspectable.
5. Use structured outputs for all LLM calls.
6. Add logging and traceability from the first milestone.
7. Prefer simple baselines before advanced agent behavior.
8. Keep components modular with clear boundaries.
9. Write code that is easy to understand, extend, and debug.
10. Add tests where they protect core behavior.
11. Explain tradeoffs before introducing architectural complexity.
12. Every major component must have an answer to: "Is this working?"
