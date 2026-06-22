# SceneScout Diagrams

Open this file in Cursor and press **Cmd+Shift+V** (Mac) or **Ctrl+Shift+V** (Windows/Linux)
to preview both diagrams.

Source files (edit these first, then sync the fenced blocks below):

- [system_architecture.mmd](./system_architecture.mmd)
- [data_flow.mmd](./data_flow.mmd)

---

## System Architecture

```mermaid
%%{init: {'theme': 'base', 'themeVariables': {'primaryColor': '#1e1e2e', 'primaryTextColor': '#cdd6f4', 'primaryBorderColor': '#89b4fa', 'lineColor': '#89b4fa', 'secondaryColor': '#181825', 'tertiaryColor': '#313244'}}}%%
flowchart TD
    subgraph INFRA["Infrastructure"]
        MODAL["Modal Scheduler\nweekly cron"]
    end

    subgraph WEB["scene-scout-web"]
        WEBUI["FastAPI Web UI\nonboarding + profile\nHTTP Basic auth"]
        TR["GET /track\nclick signal + redirect"]
        FB["GET /feedback\nnegative signal"]
    end

    subgraph PIPELINE["scene-scout-pipeline"]
        ORCH["Orchestrator\nrun_id + PipelineState"]

        subgraph PHASE1["Phase 1 — Ingest and Normalize"]
            FS["Feed Scout\nasync concurrent\nETag 304"]
            EX["Event Extraction\nLiteLLM"]
            NO["Event Normalization\ndeterministic"]
            DD["Deduplication\nexact → fuzzy → LLM"]
            DQ["Description Quality\nrubric, no LLM"]
            GEO["Geocoding\nNominatim"]
            FI["Pre-Enrichment Filter"]
        end

        subgraph BATCH["Batch Enrichment — BatchStrategy"]
            TS["Talent Scout\nperformer affinity"]
            VC["Vibe Classifier\natmosphere tags"]
            NS["Neighborhood Scout\nhyper-local 1km"]
        end

        subgraph PHASE2["Phase 2 — Rank and Send"]
            UP["User Preference Agent\nLiteLLM + delta logic"]
            RK["Ranking Agent\ndeterministic scoring\nLiteLLM explanations"]
            SO["Sell-Out Risk\nheuristic classifier"]
            CU["Recommendation Curator\nAllegra"]
            EC["Email Composer\nLiteLLM + Resend"]
        end

        EV["Evaluation Agent\nLiteLLM-as-judge"]
    end

    subgraph SERVICES["Shared Services — implemented"]
        LLM["services/llm.py\ncomplete()"]
        PL["services/prompt_loader.py\nrender_prompt()"]
        BS["services/batch.py\nBatchStrategy"]
        CS["services/cache.py\nSQLite TTL cache"]
    end

    subgraph SERVICES_PLANNED["Shared Services — planned"]
        CH["services/chroma.py\nembeddings"]
        FBS["services/feedback.py"]
        HS["services/history.py"]
    end

    subgraph VOLUMES["Persistent Volumes"]
        VP["vol-profiles\nUserProfile JSON"]
        VC2["vol-chroma\nliked-event embeddings"]
        VFB["vol-feedback\nFeedbackEvent SQLite"]
        VH["vol-history\nhistory SQLite"]
        VCA["vol-cache\nenrichment + seen_entries"]
        VPS["vol-pipeline-state\nPipelineState JSON"]
        VL["vol-logs\nJSONL 90-day rolling"]
    end

    MODAL --> ORCH
    ORCH --> PHASE1
    FS --> EX --> NO --> DD --> DQ --> GEO --> FI
    FS -.->|seen_entries| CS
    CS -.-> VCA
    FI -->|cache check| CS
    GEO -.->|venue cache| CS
    FI -->|submit batch| BS
    BS --> BATCH
    BATCH -.->|write| VCA
    VPS <-->|PipelineState| ORCH
    BATCH -->|apply results| PHASE2
    VP --> RK
    VC2 --> RK
    RK --> SO --> CU
    HS <--> CU
    CU --> EC
    EC -->|Resend| USER(("User\nUSER_EMAIL"))

    USER -->|event link| TR
    USER -->|not for me| FB
    TR --> FBS --> VFB
    FB --> FBS
    VFB --> UP
    UP --> VP
    UP --> CH --> VC2

    WEBUI <--> VP
    WEBUI <--> VFB
    WEBUI <--> VH
    WEBUI <--> VL
    EV --> VL

    LLM -.->|used by| EX
    LLM -.->|used by| BATCH
    LLM -.->|used by| RK
    LLM -.->|used by| EC
    LLM -.->|used by| UP
    LLM -.->|used by| EV
    PL -.->|used by| LLM
```

---

## Data Flow

```mermaid
%%{init: {'theme': 'base', 'themeVariables': {'primaryColor': '#1e1e2e', 'primaryTextColor': '#cdd6f4', 'primaryBorderColor': '#89b4fa', 'lineColor': '#89b4fa', 'secondaryColor': '#181825', 'tertiaryColor': '#313244'}}}%%
flowchart TD
    RSS["RSS Feeds\nFeedConfig list"]

    RSS --> A1["1. Feed Scout\nout: RawFeedEntry list\n     FeedHealthReport list"]

    A1 -->|"seen_entries hit → skip extraction"| CACHE_SEEN["seen_entries cache\nNormalizedEvent by feed_id + entry_hash"]
    CACHE_SEEN --> A3B["reuse NormalizedEvent\nbypass extraction + normalization"]

    A1 -->|"cache miss"| A2["2. Event Extraction\nin:  RawFeedEntry list\nout: EventCandidate list\n  title, venue, date, city\n  is_event bool\n  extraction_confidence float"]

    A2 -->|"valid events"| A3["3. Event Normalization\nout: NormalizedEvent list\n  id SHA-256 hash\n  start_datetime\n  price_cents optional\n  is_free bool"]

    A2 -->|"is_event=False"| DISCARD_EXT["discarded at extraction"]

    A3 --> A4
    A3B --> A4["4. Deduplication\nout: NormalizedEvent list deduped\n  exact → fuzzy → embedding → LLM\n  merge_log"]

    A4 --> A5["5. Description Quality\nout: NormalizedEvent list\n  description_quality_score float\n  low_information bool"]

    A5 --> GEO["Nominatim Geocoding\nout: venue_coordinates lat/lon\n  poi_list within 1km"]

    GEO --> FILTER["Pre-Enrichment Filter\nenrichment candidates only"]

    FILTER -->|"low_information discard\noutside week discard\n2-week window discard"| DISCARD["discarded events"]

    FILTER -->|"enrichment cache hit skip LLM\ncache miss → batch"| BATCH["BatchStrategy\nAnthropicBatch or ConcurrentAsync\nsingle submission per run"]

    BATCH --> A6["EnrichedEvent\n  performers PerformerInfo list\n  top_performer_affinity float\n  vibe_tags 2-5 strings\n  neighborhood_context optional\n  neighborhood_confidence float\n  venue_coordinates optional"]

    PROF["UserProfile\n  name, email\n  category_weights map\n  vibe_preferences list\n  excluded_categories list"] --> A7

    CHROMA["Chroma Index vol-chroma\nliked-event embeddings"] --> A7

    A6 --> A7["6. Ranking Agent\nout: RankedEvent list\n  score float\n  score_breakdown:\n    category_fit, vibe_fit\n    semantic_similarity\n    performer_affinity, location\n    novelty, source_quality\n    description_quality\n  explanation string LLM grounded\n  wildcard_slot bool"]

    A7 --> A8["7. Sell-Out Risk\nout: RankedEvent list\n  sellout_risk low medium high"]

    HIST["Recommendation History vol-history\nrecency penalties applied"] --> A9

    A8 --> A9["8. Recommendation Curator Allegra\nout: CuratedRecommendation max 10\n  rank int\n  explanation string intact\n  neighborhood_context optional\n  sellout_urgency_note optional\n  feedback_token UUID\n  is_wildcard bool"]

    A9 --> A10["9. Email Composer\nout: HTML email via Resend\n  tracking links per recommendation\n    /track token click redirect\n    /feedback token negative signal\n  UAT subject prefix with run_id"]

    A10 --> EMAIL["User Email USER_EMAIL Modal Secret"]

    EMAIL -->|"click event link"| SIG1["FeedbackEvent click\n  token, event_id, categories\n  score_breakdown, rank, run_id"]

    EMAIL -->|"not for me"| SIG2["FeedbackEvent negative"]

    SIG1 --> FBSTORE["vol-feedback SQLite"]
    SIG2 --> FBSTORE

    FBSTORE --> UPA["User Preference Agent\n  decay-weighted delta e^-lambda t\n  half-life 30 days\n  category_weights updated\n  vibe_preferences updated\n  Chroma index updated on clicks"]

    UPA --> PROF
    UPA --> CHROMA

    A10 --> EVAL["10. Evaluation Agent\nout: quality report\n  overall_quality float\n  flagged_recommendations\n  list_level_issues"]
```
