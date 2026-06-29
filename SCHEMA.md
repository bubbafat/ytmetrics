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
| `channels` | `channel_id` | title, `uploads_playlist_id`, `last_successful_pull`, `data_through` (freshness) |
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
| `video_topics` | `(video_id, topic)` | user-maintained tagging; never written by the pull, only by the owner; `added_at` |
| `revision_log` | `id` | append-only audit of changed values |

Table names carry their grain explicitly: `channel_*` aggregate to the channel, `video_*`
are per-video. The video-grain fact tables get secondary indexes on `(date)` and
`(video_id)` since they grow large.

### Content type / Shorts vs long-form
`channel_daily.creator_content_type` and `videos.content_type` come straight from the API
(`creatorContentType` dimension) — not guessed from duration. `engaged_views` is stored
alongside `views` because Shorts count views differently from long-form.

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

## Analysis recipes

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

## Known limits
- **Community Posts** are not in the YouTube Analytics API → not captured.
- Revenue / thumbnail metrics are best-effort: empty if the channel isn't monetized or the
  metric isn't available; they never fail the core pull. Per-video monetary and discovery
  reports (`video_revenue_daily`, `video_discovery_daily`) are especially prone to being
  rejected by the API for a given channel — they degrade gracefully when that happens.
- `video_referrers` has no `day` dimension and is capped at top-25 per source type.
