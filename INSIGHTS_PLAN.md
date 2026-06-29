# Insights plan — fill every API-supported analytics gap

Goal: extend ytmetrics so the [ten channel questions](#question-board) we can't fully answer
today become answerable **from the db**, for every gap the YouTube Analytics API actually
supports. Schema v2 already added per-video revenue/traffic/discovery. This plan is "phase 3":
the **aggregate / windowed insight reports**.

## The one architectural idea

Everything here is an **aggregate over a window**, not a daily time series. These reports
(`ageGroup`, `country`, `deviceType`, retention curves, search terms) either don't compose
with the `day` dimension or are only meaningful as a period rollup. So they all use the
**windowed-fact shape we already have for `video_referrers`** — `window_start` / `window_end`
in the PK, `pulled_at` timestamp — not the `*_daily` shape.

Consequence: they do **not** belong in the daily `pull`. Add a separate **`ytmetrics insights
--window A:B`** command (mirrors the existing `referrers` command) run on a slower cadence
(weekly/monthly). The one exception is `subscribedStatus`, which *does* compose with `day` and
can ride the daily pull.

All new reports follow the existing guarded/degrade pattern: on `HttpError`, log + append to
`batch.degraded`, never break anything.

---

## Work items

Ordered by leverage. Each lists the API report (verified against Google's docs), the new
table(s), grain, cadence, API-call cost, and which question it flips.

### W1 — Retention curves  →  flips #4 (Partial → **Yes**)
- **Report:** `dimensions=elapsedVideoTimeRatio`, `metrics=audienceWatchRatio,relativeRetentionPerformance`, `filters=video==<ID>;audienceType==ORGANIC`. Returns ~100 points (ratio 0.01→1.0) per video. **One video per call — no comma-batching.**
- **Table:** `video_retention(channel_id, video_id, elapsed_ratio, audience_type, audience_watch_ratio, relative_retention_performance, window_start, window_end, pulled_at)`, PK `(channel_id, video_id, audience_type, elapsed_ratio, window_start, window_end)`.
- **Grain:** per video. **Cadence:** weekly, and worth limiting to videos above a view floor (retention on a 12-view video is noise).
- **Budget:** 1 call/video (~81 now). Also pull `audienceType==ORGANIC` only first; add `AD_INDUCED`/`SUBSCRIBED` later if wanted.
- **Payoff:** the single most useful editing/packaging signal — where viewers actually drop, and whether a video beats peers of its length (`relativeRetentionPerformance`).

### W2 — Demographics  →  flips #8 (No → **Yes**, demographics half)
- **Report:** `dimensions=ageGroup,gender`, `metrics=viewerPercentage`. Optional `subscribedStatus`. Per-video via `filters=video==<ID>`.
- **Table:** `channel_demographics(channel_id, age_group, gender, subscribed_status, viewer_percentage, window_start, window_end, pulled_at)`; optional sibling `video_demographics(... , video_id, ...)` later. Keep grain explicit in the name (don't mix channel + video rows in one table).
- **Grain:** channel first (who watches the channel), per-video as a stretch. **Cadence:** monthly. **Budget:** ~1–2 calls.
- **Note:** `viewerPercentage` is a share, not additive — never `SUM` across videos.

### W3 — Geography  →  flips #8 (geography half)
- **Report:** `dimensions=country`, `metrics=views,estimatedMinutesWatched,averageViewPercentage`.
- **Table:** `audience_geography(channel_id, country, views, estimated_minutes_watched, average_view_percentage, window_start, window_end, pulled_at)`.
- **Grain:** channel (per-video optional). **Cadence:** monthly. **Budget:** 1 call. Useful for "is my audience US-heavy?" — relevant to RPM (US monetizes higher).

### W4 — Devices / OS  →  flips #8 (device half)
- **Report:** `dimensions=deviceType,operatingSystem`, `metrics=views,estimatedMinutesWatched`.
- **Table:** `audience_devices(channel_id, device_type, operating_system, views, estimated_minutes_watched, window_start, window_end, pulled_at)`.
- **Cadence:** monthly. **Budget:** 1 call. Lowest priority of the audience set, but cheap.

### W5 — Subscribed vs. unsubscribed viewing  →  flips #9 (Partial → **Yes**, the API half)
- **Report:** add the `subscribedStatus` dimension to the standard activity query. It **composes with `day`**, so this can be a **daily** table on the normal pull.
- **Table:** `subscribed_status_daily(channel_id, date, subscribed_status, views, estimated_minutes_watched, last_updated)`, PK `(channel_id, date, subscribed_status)`. Daily shape, revisioned.
- **Payoff:** loyalty proxy that's far better than what we infer today — what share of views/watch-time comes from subscribers vs. non-subscribers, over time. (True "new vs returning unique viewers" is still Studio-only — see out-of-scope.)

### W6 — Search terms & traffic-source detail  →  deepens #2 / #6
- **Report:** `dimensions=insightTrafficSourceDetail`, `metrics=views,estimatedMinutesWatched`, `filters=insightTrafficSourceType==YT_SEARCH` (and `PLAYLIST`, `SUBSCRIBER`, etc.). Top-N, windowed — exactly the `referrers` pattern (which already covers `RELATED_VIDEO`/`END_SCREEN`).
- **Table:** `traffic_source_detail(channel_id, video_id, traffic_source_type, detail, views, estimated_minutes_watched, window_start, window_end, pulled_at)` — generalizes `video_referrers`; or a focused `search_terms` table if we only want YT_SEARCH.
- **Payoff:** the actual queries people use to find you — gold for titling/packaging and for a Rough Cut episode.

### W7 — Subscriber-count anchor  →  closes #1's only gap
- **Source:** Data API `channels.list(part=statistics)` → `subscriberCount` (a current total, not Analytics). Pull once per run.
- **Change:** add `subscriber_count` to the `channels` dim (or a tiny daily snapshot). Lets us turn the daily gained/lost deltas into an absolute curve.
- **Budget:** 1 call, already partly done (we call `channels.list`).

---

## Sequencing

1. **W5 (subscribed-status)** + **W7 (sub anchor)** — smallest, ride the daily pull, immediate wins on #9 and #1.
2. **W1 (retention)** — highest analytic value; needs the new `insights` command + per-video call budget.
3. **W2–W4 (demographics / geography / devices)** — one `insights` pass, monthly cadence.
4. **W6 (search terms)** — extends the existing referrers machinery.

## Plumbing each item needs (same pattern as the v2 refactor)

- **schema.py:** a new `TableSpec` (windowed PK for W1–W4/W6; daily for W5); index on `(video_id)` where per-video; rollup views as useful.
- **normalize.py:** a row builder (most are just `normalize_rows(resp, {...})` — `ageGroup`/`gender`/`country`/`deviceType`/`operatingSystem`/`insightTrafficSourceDetail`/`elapsedVideoTimeRatio` need adding to `API_TO_COL`).
- **live.py:** a guarded query; W1 loops per video.
- **cli.py:** new `insights --window` subcommand for the windowed reports (W1–W4, W6); W5 folds into `pull`.
- **migrations.py:** bump `CURRENT_SCHEMA_VERSION`; tables are additive (no drops).
- **replay.py + fixtures + tests:** offline coverage for each, like we did in v2.

## Explicitly NOT API-supported (don't chase these)

- **Per-video thumbnail-impression CTR** — confirmed null at video grain on our last pull; YouTube doesn't serve it per video. (#3 stays Partial.)
- **True new-vs-returning unique viewers / frequency** — Studio only. (`subscribedStatus` in W5 is the closest API proxy.)
- **Audience overlap ("what else my viewers watch")** — Studio only. (#8's last sliver.)
- **A/B thumbnail Test-and-Compare CTR** — Studio only.
- **Topic tagging (#6)** — not an API gap at all; it's hand-curated `video_topics` (already a table, just empty). Owner action, not a pull.

## Question board — after this plan

| # | Question | Today | After plan |
|---|---|---|---|
| 1 | Growth trajectory | Yes+ | **Yes** (absolute anchor, W7) |
| 2 | Discovery mix / algo push | Yes+ | **Yes+** (+ search terms, W6) |
| 3 | Packaging / thumbnail CTR | Partial | Partial *(API limit)* |
| 4 | Retention / where they drop | Partial | **Yes** (W1) |
| 5 | Subscriber conversion | Yes+ | Yes+ |
| 6 | Topics / formats to double down | Partial | format **Yes**; topic = owner tagging |
| 7 | Long tail / concentration | Yes++ | Yes++ |
| 8 | Who is our audience | No | **Yes** (W2–W4); overlap still Studio-only |
| 9 | Loyalty / returning | Partial | **Yes** (W5); true uniques still Studio-only |
| 10 | Monetization drivers | Yes | Yes |

After this, every box the API can fill is **Yes**; the only remaining Partials/Nos are the four
Studio-only items above plus owner-side topic tagging.
