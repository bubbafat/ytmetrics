# ytmetrics data model

The SQLite database (`ytmetrics.db`) is the system of record. It is a single portable
file you can query with `sqlite3`, DuckDB (`ATTACH`), pandas, or hand to Claude. All dates
are **Pacific Time** (`America/Los_Angeles`) because that is how YouTube defines a "day".

Schema version lives in `meta.schema_version` and is upgraded automatically by a tiny
migration runner (`store/migrations.py`).

## Conventions

- Daily metric tables upsert with `INSERT … ON CONFLICT(pk) DO UPDATE SET col =
  COALESCE(excluded.col, table.col)`: a NULL incoming value never overwrites an existing
  value (retention), and keys absent from a pull are left untouched.
- `last_updated` / `pulled_at` record when a row was last written.
- A changed value (old non-NULL → new) is appended to `revision_log` when the channel has
  `track_revisions = true`.

## Tables

| table | grain (primary key) | notes |
|-------|---------------------|-------|
| `meta` | `key` | `schema_version` + tool metadata |
| `channels` | `channel_id` | title, `uploads_playlist_id`, `last_successful_pull`, `data_through` (freshness), `subscriber_count` (current absolute total from the Data API `channels.list(statistics)`, refreshed each pull) |
| `videos` | `video_id` | title, `published_at`, `duration_seconds`, `privacy_status`, `content_type` (SHORTS/VIDEO_ON_DEMAND/LIVE_STREAM/STORY) |
| `channel_daily` | `(channel_id, date, creator_content_type)` | per-day split by content type; `views`, `engaged_views`, `red_views`, watch-time, subs, engagement |
| `channel_discovery_daily` | `(channel_id, date)` | best-effort thumbnail/card impressions + CTR |
| `channel_traffic_sources_daily` | `(channel_id, date, traffic_source_type)` | views/watch-time by source (Shorts feed, search, ADVERTISING, …) |
| `channel_revenue_daily` | `(channel_id, date)` | optional; revenue, CPM/RPM, `monetized_playbacks`, `ad_impressions` |
| `video_daily` | `(channel_id, video_id, date)` | per-video per-day metrics incl. `content_type`, `engaged_views` |
| `video_revenue_daily` | `(channel_id, video_id, date)` | optional per-video revenue (join `videos` for `content_type`); may degrade if the API rejects per-video monetary reports |
| `video_traffic_sources_daily` | `(channel_id, video_id, date, traffic_source_type)` | per-video views/watch-time by source |
| `video_discovery_daily` | `(channel_id, video_id, date)` | best-effort per-video thumbnail/card impressions + CTR |
| `video_referrers` | `(channel_id, dest_video_id, traffic_source_type, referrer_detail, window_start, window_end)` | on-demand referral attribution; `referrer_video_id` is the referring video |
| `subscribed_status_daily` | `(channel_id, date, subscribed_status)` | views/watch-time split by `subscribedStatus` (SUBSCRIBED/UNSUBSCRIBED); rides the daily `pull` (composes with `day`) |
| `video_retention` | `(channel_id, video_id, audience_type, elapsed_ratio, window_start, window_end)` | windowed; retention curve — `audience_watch_ratio` + `relative_retention_performance` per elapsed-time ratio (`audience_type='ORGANIC'`); 1 API call/video |
| `channel_demographics` | `(channel_id, age_group, gender, subscribed_status, window_start, window_end)` | windowed; `viewer_percentage` share by age/gender (`subscribed_status='ALL'` for the v1 query). A share, not additive — never `SUM` across rows |
| `audience_geography` | `(channel_id, country, window_start, window_end)` | windowed; views/watch-time/`average_view_percentage` by country |
| `audience_devices` | `(channel_id, device_type, operating_system, window_start, window_end)` | windowed; views/watch-time by device + OS |
| `traffic_source_detail` | `(channel_id, video_id, traffic_source_type, detail, window_start, window_end)` | windowed top-25; the actual search terms (`YT_SEARCH`) / playlists per source type. `video_id=''` for channel-level |
| `video_topics` | `(video_id, topic)` | user-maintained tagging; never written by the pull, only by the owner; `added_at` |
| `revision_log` | `id` | append-only audit of changed values |

Table names carry their grain explicitly: `channel_*` aggregate to the channel, `video_*`
are per-video. The video-grain fact tables get secondary indexes on `(date)` and
`(video_id)` since they grow large.

### Content type / Shorts vs long-form
`channel_daily.creator_content_type` and `videos.content_type` come straight from the API
(`creatorContentType` dimension) — not guessed from duration. `engaged_views` is stored
alongside `views` because Shorts count views differently from long-form.

### Windowed insights (`ytmetrics insights --window A:B`)
`video_retention`, `channel_demographics`, `audience_geography`, `audience_devices`, and
`traffic_source_detail` are **aggregate-over-a-window** facts (no `day` dimension) — same
shape as `video_referrers` (`window_start`/`window_end` in the PK, `pulled_at` timestamp).
They are pulled on a slower cadence by the separate `insights` command, not the daily
`pull`, and each report degrades gracefully (logged + skipped) if the API rejects it for a
channel:

```
ytmetrics insights --window 2026-06-01:2026-06-30                # explicit window
ytmetrics insights --days 90                                     # trailing window (for automation)
ytmetrics insights --window 2026-06-01:2026-06-30 --channel main --no-demographics
```

Run it on a slower cadence than the daily `pull` — retention costs one API call per video,
and these are period aggregates. The repo ships a weekly launchd example
(`scheduling/com.ytmetrics.weekly.plist.example`, `insights --days 90`).

Each run **appends a whole new snapshot** (keyed by `window_start`/`window_end`), so these
tables accumulate one snapshot per run — keeping a rolling history lets you see how
retention/demographics shift over time. To bound that growth, `insights` prunes snapshots
older than `insights_retention_weeks` (config, default **26**; set `0` to keep everything)
after each run. Only the `INSIGHT_SNAPSHOT_TABLES` are pruned; the daily fact tables and
`video_referrers` are never touched.

`subscribed_status_daily` is the one insight that **composes with `day`**, so it rides the
normal daily `pull` instead.

## Views (stable analysis surfaces)

| view | what it gives |
|------|---------------|
| `v_channel_daily_totals` | channel totals per day (sums across content types), incl. `net_subscribers` |
| `v_shorts_vs_long` | per day, `bucket` = shorts / long_form |
| `v_video_lifetime` | per-video lifetime totals joined to title/content_type |
| `v_video_revenue_lifetime` | per-video lifetime revenue (from `video_revenue_daily`) joined to title/content_type |
| `v_view_monetization` | per day: `premium_views` (Premium), `ad_monetized_playbacks`, and `approx_non_monetized_views` (proxy — views and playbacks are different units) |
| `v_paid_vs_organic` | per day, `bucket` = paid (ADVERTISING/PROMOTED) / organic |
| `v_revenue_attribution` | per `(channel_id, date)`: `channel_revenue`, `attributed_revenue` (sum of `video_revenue_daily`), and `unattributed_revenue` (the residual) |

> **Channel totals ≠ sum of videos.** YouTube's channel-level `estimated_revenue`
> legitimately exceeds the sum of its per-video revenue (some revenue isn't attributable to
> a single video). `v_revenue_attribution` exposes that residual as `unattributed_revenue`
> rather than hiding it — don't expect the per-video numbers to add up to the channel total.

## How to reason with this data

The store is **two grains × two cadences**, plus dimension tables and pre-baked views. Get
this model right and the queries follow; get it wrong and you'll silently mix incompatible rows.

- **Grain** is in the table name: `channel_*` = the whole channel, `video_*` = per video.
- **Cadence**:
  - `*_daily` — one row per day. **Safe to `SUM` across dates.**
  - **windowed** (`window_start`/`window_end` in the PK: `video_retention`, `channel_demographics`,
    `audience_geography`, `audience_devices`, `traffic_source_detail`, `video_referrers`) — an
    aggregate **over a period**. Read the *latest* snapshot via `MAX(window_end)`; **never `SUM`
    across windows**, and never join two different windows expecting them to add up.
- **Dimensions**: join facts to `videos` (title, `content_type`, `published_at`,
  `duration_seconds`), `channels` (`subscriber_count`, freshness), and `video_topics` (the owner's
  hand-applied tags) for labels and segments.
- **Views** (`v_*`) are the pre-baked rollups — prefer them for common aggregations.

### Gotchas to internalize before trusting a number

1. **Lifetime vs. monetized window.** `channel_daily` / `video_daily` span the channel's *entire
   life*; the revenue tables only start at *monetization*. To compute RPM or any "since we
   monetized" figure, **filter the daily tables to the monetized window**, e.g.
   `date >= (SELECT MIN(date) FROM channel_revenue_daily)` (Empty Besters monetized `2026-06-03`).
   Summing lifetime views against monetized revenue produces nonsense (a fraction-of-a-cent RPM).
2. **Channel ≠ sum of videos.** Channel totals legitimately exceed the sum of per-video rows; use
   `v_revenue_attribution` and treat `unattributed_revenue` as real, not a bug.
3. **Recent days are estimates.** Dates are Pacific; YouTube revises roughly the last 2–3 days. For
   "final" numbers prefer rows older than ~3 days, consult `revision_log`, and check
   `channels.data_through` for freshness.
4. **Shorts ≠ long-form.** Segment by `creator_content_type` (channel) / `videos.content_type`
   (video). They count views differently (`engaged_views`) and Shorts barely monetize — never blend
   them into one average.
5. **Percentages and rates aren't additive.** `viewer_percentage`, `average_view_percentage`, CTR,
   CPM/RPM — never `SUM`; weight by `views`/`impressions` if you must combine.
6. **Premium (red) views** monetize via subscription, not ads (`red_views`,
   `estimated_red_partner_revenue`); `v_view_monetization` separates them.
7. **Empty isn't always zero.** Per-video revenue/discovery and the windowed insight reports degrade
   to *empty* when the API rejects them for a channel — confirm the table has rows before concluding
   "zero".

Reusable idiom — *the latest snapshot of a windowed table*:
```sql
SELECT * FROM video_retention
WHERE channel_id = :cid
  AND window_end = (SELECT MAX(window_end) FROM video_retention WHERE channel_id = :cid);
```

## Recipe cookbook — a query per question

**Week-over-week channel growth**
```sql
SELECT date, views, net_subscribers
FROM v_channel_daily_totals
WHERE channel_id = 'UC…' ORDER BY date;
```

**Shorts vs long-form contribution**
```sql
SELECT bucket, SUM(views) views, SUM(net_subscribers) net_subs
FROM v_shorts_vs_long WHERE channel_id = 'UC…' GROUP BY bucket;
```

**Short → long-form impact (before/after lift).** Compare the destination video's daily
views around the linking Short's publish date:
```sql
WITH pub AS (SELECT published_at FROM videos WHERE video_id = '<short_id>')
SELECT CASE WHEN vd.date < substr((SELECT published_at FROM pub),1,10)
            THEN 'before' ELSE 'after' END AS period,
       AVG(vd.views) avg_views, AVG(vd.subscribers_gained) avg_subs
FROM video_daily vd
WHERE vd.video_id = '<long_form_id>'
GROUP BY period;
```
Then run direct attribution and read `video_referrers`:
```
ytmetrics referrers --video <long_form_id> --window 2026-05-01:2026-05-31   # before
ytmetrics referrers --video <long_form_id> --window 2026-06-01:2026-06-30   # after
```
```sql
SELECT window_start, window_end, traffic_source_type, referrer_video_id, views
FROM video_referrers
WHERE dest_video_id = '<long_form_id>' AND referrer_video_id = '<short_id>'
ORDER BY window_start;
```
The before/after lift is correlational; the referrer attribution is direct (but a windowed
top-25, no daily breakdown). Together they're a defensible read of the Short's impact.

**Per-video revenue leaderboard + concentration** *(who actually earns; fragility)*
```sql
SELECT v.title,
       ROUND(SUM(vr.estimated_revenue), 2)                                       AS rev,
       ROUND(100.0 * SUM(vr.estimated_revenue) / SUM(SUM(vr.estimated_revenue)) OVER (), 1) AS pct
FROM video_revenue_daily vr JOIN videos v USING (video_id)
GROUP BY vr.video_id ORDER BY rev DESC LIMIT 10;
```

**RPM by content type** *(Shorts vs long-form earning power — note the monetized-window filter)*
```sql
WITH rev AS (SELECT video_id, SUM(estimated_revenue) r FROM video_revenue_daily GROUP BY 1),
     vw  AS (SELECT video_id, SUM(views) v FROM video_daily
             WHERE date >= (SELECT MIN(date) FROM channel_revenue_daily) GROUP BY 1)
SELECT vi.content_type,
       ROUND(SUM(rev.r), 2)                              AS revenue,
       SUM(vw.v)                                         AS views,
       ROUND(1000.0 * SUM(rev.r) / NULLIF(SUM(vw.v), 0), 2) AS rpm
FROM videos vi LEFT JOIN rev USING (video_id) LEFT JOIN vw USING (video_id)
GROUP BY vi.content_type;
```

**Which videos convert viewers to subscribers** *(subs per 1k views, not vanity)*
```sql
SELECT v.title, SUM(vd.views) AS views, SUM(vd.subscribers_gained) AS subs,
       ROUND(1000.0 * SUM(vd.subscribers_gained) / NULLIF(SUM(vd.views), 0), 1) AS subs_per_1k
FROM video_daily vd JOIN videos v USING (video_id)
GROUP BY vd.video_id HAVING views > 100 ORDER BY subs_per_1k DESC LIMIT 10;
```

**Discovery mix over time** *(is suggested/browse rising = the algorithm promoting you?)*
```sql
SELECT substr(date, 1, 7) AS month, traffic_source_type, SUM(views) AS views
FROM channel_traffic_sources_daily
GROUP BY month, traffic_source_type ORDER BY month, views DESC;
```

**Retention cliffs for a video** *(the 5 steepest drop-offs — where the edit loses people)*
```sql
WITH r AS (
  SELECT elapsed_ratio, audience_watch_ratio,
         audience_watch_ratio - LAG(audience_watch_ratio) OVER (ORDER BY elapsed_ratio) AS delta
  FROM video_retention
  WHERE video_id = :vid
    AND window_end = (SELECT MAX(window_end) FROM video_retention WHERE video_id = :vid))
SELECT elapsed_ratio, ROUND(audience_watch_ratio, 3) AS watch, ROUND(delta, 3) AS drop_off
FROM r WHERE delta IS NOT NULL ORDER BY delta ASC LIMIT 5;
```

**Who is the audience** *(latest demographics + geography snapshots)*
```sql
SELECT age_group, gender, ROUND(viewer_percentage, 1) AS pct
FROM channel_demographics
WHERE window_end = (SELECT MAX(window_end) FROM channel_demographics)
ORDER BY pct DESC LIMIT 8;

SELECT country, views FROM audience_geography
WHERE window_end = (SELECT MAX(window_end) FROM audience_geography)
ORDER BY views DESC LIMIT 8;
```

**Loyalty: subscribed vs. unsubscribed viewing** *(the API's best returning-viewer proxy)*
```sql
SELECT substr(date, 1, 7) AS month, subscribed_status, SUM(views) AS views
FROM subscribed_status_daily GROUP BY month, subscribed_status ORDER BY month;
```

**What people search to find you** *(titling/packaging gold)*
```sql
SELECT detail AS search_term, views
FROM traffic_source_detail
WHERE traffic_source_type = 'YT_SEARCH'
  AND window_end = (SELECT MAX(window_end) FROM traffic_source_detail
                    WHERE traffic_source_type = 'YT_SEARCH')
ORDER BY views DESC LIMIT 20;
```

**Evergreen vs. spike-and-die** *(views by days-since-publish — a flat tail = evergreen)*
```sql
SELECT vd.video_id, v.title,
       CAST(julianday(vd.date) - julianday(substr(v.published_at, 1, 10)) AS INT) AS day_n,
       vd.views
FROM video_daily vd JOIN videos v USING (video_id)
ORDER BY vd.video_id, day_n;
```

**Performance by topic** *(requires `video_topics` to be populated — the owner's tagging)*
```sql
SELECT t.topic, COUNT(DISTINCT vl.video_id) AS videos, SUM(vl.views) AS views,
       ROUND(SUM(vrl.estimated_revenue), 2) AS revenue
FROM video_topics t
JOIN v_video_lifetime vl USING (video_id)
LEFT JOIN v_video_revenue_lifetime vrl USING (video_id)
GROUP BY t.topic ORDER BY views DESC;
```

## Known limits
- **Community Posts** are not in the YouTube Analytics API → not captured.
- Revenue / thumbnail metrics are best-effort: empty if the channel isn't monetized or the
  metric isn't available; they never fail the core pull. Per-video monetary and discovery
  reports (`video_revenue_daily`, `video_discovery_daily`) are especially prone to being
  rejected by the API for a given channel — they degrade gracefully when that happens.
- `video_referrers` has no `day` dimension and is capped at top-25 per source type.
- The windowed insight tables (`video_retention`, `channel_demographics`,
  `audience_geography`, `audience_devices`, `traffic_source_detail`) likewise have no `day`
  dimension — they are period rollups, pulled by `insights`. `traffic_source_detail` is a
  windowed top-25 per source type. Any of these (and `subscribed_status_daily`) may degrade
  for a given channel if the API doesn't serve the report; retention is the heaviest
  (1 call per video).
- `channel_demographics.viewer_percentage` is a share, not a count — never `SUM` it.
- True new-vs-returning unique viewers, audience overlap, and per-video thumbnail CTR are
  Studio-only and not in the Analytics API → not captured.
