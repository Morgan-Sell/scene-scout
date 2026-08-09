# Sell-Out Risk ML Model (Future Design)

SceneScout v1 uses a **deterministic heuristic classifier** in
`scene_scout/agents/sellout_risk.py`. It assigns `low`, `medium`, or `high`
bands and sets `sellout_urgency_note` for every `high` event (see
`sellout_risk_config.HIGH_RISK_URGENCY_NOTE`). Email Composer surfaces that note
in the weekly message.

This document describes a future **ML upgrade path** once enough behavioral and
inventory signals exist. It is not implemented in v1.

## Goals

- Predict probability that an event will sell out before showtime.
- Drive `sellout_risk` bands and optional urgency copy with calibrated scores.
- Improve over heuristics for mainstream inventory (Ticketmaster, large venues)
  where description language is marketing-heavy and venue tokens are noisy.

## Training signal (labels)

| Label source | Strength | Notes |
|---|---|---|
| Post-event inventory scrape | Strong | Did the primary ticket URL show sold out / limited? |
| User click + no conversion | Weak negative | Click without purchase may mean price friction, not availability |
| Explicit "sold out" in feed refresh | Strong | Requires periodic re-fetch of event URLs |
| Heuristic v1 band | Bootstrap | Use current classifier as a weak teacher until real labels accumulate |

Target: binary `sold_out_before_start` within a 7-day prediction window, or a
regression on `remaining_inventory_pct` when inventory APIs become available.

## Feature set (candidate)

**Event features**

- Venue capacity bucket (from external venue database, not name tokens)
- Price tier, free flag, on-sale age
- Days until event, day-of-week, holiday proximity
- Category, performer draw (Talent Scout affinity), source feed quality
- Description urgency phrase count (existing heuristic signal)
- Cross-feed duplicate count (demand proxy)

**User / market features**

- Metro (`home_city`), seasonality for that market
- Historical sell-through rate for venue + category
- Rolling click-through rate on similar events in recommendation history

**Behavioral features (after Phase 8+)**

- Click rate on prior recommendations with same `sellout_risk` band
- Time from email send to click for high-urgency items

## Model candidates

1. **Gradient-boosted trees (LightGBM / XGBoost)** — fast iteration, handles mixed
   tabular features, good baseline for sparse early data.
2. **Logistic regression** — interpretable coefficients for debugging in Dev
   Section; useful as a shadow model.
3. **Survival / time-to-sell-out** — if timestamped inventory snapshots exist.

Start with (1) offline; export thresholds that map predicted probability to
`low` / `medium` / `high` bands aligned with current UX.

## Serving

| Phase | Serving mode |
|---|---|
| v1 (now) | Heuristic `composite_risk_score` in pipeline |
| v1.5 | Shadow ML score logged to run JSONL; bands still from heuristic |
| v2 | ML probability replaces composite score; bands from calibrated thresholds |

Inference runs in the Sell-Out Risk agent step (after Ranking, before Curator).
Keep inference **local and synchronous** — no network call on the hot path.

## Urgency copy

v1 uses a single note for all `high` events. A future model may:

- Select among a small template set keyed by predicted quantile (e.g. "Likely to
  sell out today" vs "High demand — grab tickets soon").
- Stay deterministic in template choice (no LLM on the hot path).

## Evaluation

- **Calibration**: reliability diagram on held-out weeks; Brier score.
- **Product metrics**: click-through on high-urgency rows vs medium/low; user
  negative feedback rate on urgency items (avoid alert fatigue).
- **Safety**: cap share of `high` band per weekly email (Curator already limits
  count via selection; add band-level cap if ML over-fires).

## Data storage

- Training tables: `output/{run_id}/` artifacts + append-only feature rows in
  SQLite or parquet under `vol-ml/` (TBD when Phase 9+ observability lands).
- Do not store PII beyond existing `UserProfile` fields used in ranking.

## Rollout checklist

1. Log heuristic score + band for every ranked event (already partially logged).
2. Add inventory refresh job for recommended URLs (new feed poller or one-off
   scrape).
3. Backfill labels for 4–8 weeks of UAT/live runs.
4. Train v1 model offline; compare ROC-AUC vs heuristic on holdout.
5. Shadow deploy; tune `RISK_THRESHOLD_*` equivalents on ML probability.
6. Swap classifier in `sellout_risk.run()` behind a config flag.
