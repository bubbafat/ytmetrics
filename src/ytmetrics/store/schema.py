"""The SQLite schema as data: one source of truth for DDL, upsert metadata, and views.

Each upsertable table is a ``TableSpec`` (columns, primary key, timestamp column, whether
its value changes are worth logging to revision_log). DDL is generated from the specs so
metadata and schema can't drift.
"""

from __future__ import annotations

from dataclasses import dataclass

CURRENT_SCHEMA_VERSION = 4


@dataclass(frozen=True)
class TableSpec:
    name: str
    columns: list[tuple[str, str]]  # (column_name, sql_type)
    pk: list[str]
    ts_col: str | None = None  # last_updated / pulled_at / last_seen — always set on upsert
    revisioned: bool = False  # log value changes to revision_log when tracking is on

    @property
    def col_names(self) -> list[str]:
        return [c for c, _ in self.columns]

    @property
    def value_cols(self) -> list[str]:
        """Data columns: not part of the PK and not the timestamp column."""
        skip = set(self.pk) | ({self.ts_col} if self.ts_col else set())
        return [c for c in self.col_names if c not in skip]

    def create_sql(self) -> str:
        cols_sql = ",\n  ".join(f"{name} {sqltype}" for name, sqltype in self.columns)
        pk_sql = ", ".join(self.pk)
        return (
            f"CREATE TABLE IF NOT EXISTS {self.name} (\n  {cols_sql},\n"
            f"  PRIMARY KEY ({pk_sql})\n);"
        )


# --- Dimension tables -------------------------------------------------------------

CHANNELS = TableSpec(
    name="channels",
    columns=[
        ("channel_id", "TEXT"),
        ("title", "TEXT"),
        ("uploads_playlist_id", "TEXT"),
        ("last_successful_pull", "TEXT"),
        ("data_through", "TEXT"),
        ("subscriber_count", "INTEGER"),
        ("handle", "TEXT"),  # the @handle (Data API snippet.customUrl), e.g. "@yourchannel"
    ],
    pk=["channel_id"],
)

VIDEOS = TableSpec(
    name="videos",
    columns=[
        ("video_id", "TEXT"),
        ("channel_id", "TEXT"),
        ("title", "TEXT"),
        ("published_at", "TEXT"),
        ("duration_seconds", "INTEGER"),
        ("privacy_status", "TEXT"),
        ("content_type", "TEXT"),
        ("last_seen", "TEXT"),
    ],
    pk=["video_id"],
    ts_col="last_seen",
)

# --- Daily metric tables ----------------------------------------------------------

CHANNEL_DAILY = TableSpec(
    name="channel_daily",
    columns=[
        ("channel_id", "TEXT"),
        ("date", "TEXT"),
        ("creator_content_type", "TEXT"),
        ("views", "INTEGER"),
        ("engaged_views", "INTEGER"),
        ("red_views", "INTEGER"),
        ("estimated_minutes_watched", "INTEGER"),
        ("estimated_red_minutes_watched", "INTEGER"),
        ("average_view_duration", "REAL"),
        ("average_view_percentage", "REAL"),
        ("subscribers_gained", "INTEGER"),
        ("subscribers_lost", "INTEGER"),
        ("likes", "INTEGER"),
        ("dislikes", "INTEGER"),
        ("comments", "INTEGER"),
        ("shares", "INTEGER"),
        ("last_updated", "TEXT"),
    ],
    pk=["channel_id", "date", "creator_content_type"],
    ts_col="last_updated",
    revisioned=True,
)

CHANNEL_DISCOVERY_DAILY = TableSpec(
    name="channel_discovery_daily",
    columns=[
        ("channel_id", "TEXT"),
        ("date", "TEXT"),
        ("video_thumbnail_impressions", "INTEGER"),
        ("video_thumbnail_impressions_click_rate", "REAL"),
        ("card_impressions", "INTEGER"),
        ("card_click_rate", "REAL"),
        ("last_updated", "TEXT"),
    ],
    pk=["channel_id", "date"],
    ts_col="last_updated",
    revisioned=True,
)

VIDEO_DAILY = TableSpec(
    name="video_daily",
    columns=[
        ("channel_id", "TEXT"),
        ("video_id", "TEXT"),
        ("date", "TEXT"),
        ("content_type", "TEXT"),
        ("views", "INTEGER"),
        ("engaged_views", "INTEGER"),
        ("estimated_minutes_watched", "INTEGER"),
        ("average_view_duration", "REAL"),
        ("average_view_percentage", "REAL"),
        ("likes", "INTEGER"),
        ("dislikes", "INTEGER"),
        ("comments", "INTEGER"),
        ("shares", "INTEGER"),
        ("subscribers_gained", "INTEGER"),
        ("last_updated", "TEXT"),
    ],
    pk=["channel_id", "video_id", "date"],
    ts_col="last_updated",
    revisioned=True,
)

CHANNEL_TRAFFIC_SOURCES_DAILY = TableSpec(
    name="channel_traffic_sources_daily",
    columns=[
        ("channel_id", "TEXT"),
        ("date", "TEXT"),
        ("traffic_source_type", "TEXT"),
        ("views", "INTEGER"),
        ("estimated_minutes_watched", "INTEGER"),
        ("last_updated", "TEXT"),
    ],
    pk=["channel_id", "date", "traffic_source_type"],
    ts_col="last_updated",
    revisioned=True,
)

VIDEO_REFERRERS = TableSpec(
    name="video_referrers",
    columns=[
        ("channel_id", "TEXT"),
        ("dest_video_id", "TEXT"),
        ("traffic_source_type", "TEXT"),
        ("referrer_detail", "TEXT"),
        ("window_start", "TEXT"),
        ("window_end", "TEXT"),
        ("referrer_video_id", "TEXT"),
        ("views", "INTEGER"),
        ("estimated_minutes_watched", "INTEGER"),
        ("pulled_at", "TEXT"),
    ],
    pk=[
        "channel_id",
        "dest_video_id",
        "traffic_source_type",
        "referrer_detail",
        "window_start",
        "window_end",
    ],
    ts_col="pulled_at",
)

CHANNEL_REVENUE_DAILY = TableSpec(
    name="channel_revenue_daily",
    columns=[
        ("channel_id", "TEXT"),
        ("date", "TEXT"),
        ("estimated_revenue", "REAL"),
        ("estimated_ad_revenue", "REAL"),
        ("estimated_red_partner_revenue", "REAL"),
        ("gross_revenue", "REAL"),
        ("cpm", "REAL"),
        ("playback_based_cpm", "REAL"),
        ("monetized_playbacks", "INTEGER"),
        ("ad_impressions", "INTEGER"),
        ("last_updated", "TEXT"),
    ],
    pk=["channel_id", "date"],
    ts_col="last_updated",
    revisioned=True,
)

# --- Video-grain fact tables ------------------------------------------------------
# Mirror the channel-grain facts but keyed per video. content_type is NOT denormalized
# here — join to ``videos`` for it. These tables grow large, so they get indexes below.

VIDEO_REVENUE_DAILY = TableSpec(
    name="video_revenue_daily",
    columns=[
        ("channel_id", "TEXT"),
        ("video_id", "TEXT"),
        ("date", "TEXT"),
        ("estimated_revenue", "REAL"),
        ("estimated_ad_revenue", "REAL"),
        ("estimated_red_partner_revenue", "REAL"),
        ("gross_revenue", "REAL"),
        ("cpm", "REAL"),
        ("playback_based_cpm", "REAL"),
        ("monetized_playbacks", "INTEGER"),
        ("ad_impressions", "INTEGER"),
        ("last_updated", "TEXT"),
    ],
    pk=["channel_id", "video_id", "date"],
    ts_col="last_updated",
    revisioned=True,
)

VIDEO_TRAFFIC_SOURCES_DAILY = TableSpec(
    name="video_traffic_sources_daily",
    columns=[
        ("channel_id", "TEXT"),
        ("video_id", "TEXT"),
        ("date", "TEXT"),
        ("traffic_source_type", "TEXT"),
        ("views", "INTEGER"),
        ("estimated_minutes_watched", "INTEGER"),
        ("last_updated", "TEXT"),
    ],
    pk=["channel_id", "video_id", "date", "traffic_source_type"],
    ts_col="last_updated",
    revisioned=True,
)

VIDEO_DISCOVERY_DAILY = TableSpec(
    name="video_discovery_daily",
    columns=[
        ("channel_id", "TEXT"),
        ("video_id", "TEXT"),
        ("date", "TEXT"),
        ("video_thumbnail_impressions", "INTEGER"),
        ("video_thumbnail_impressions_click_rate", "REAL"),
        ("card_impressions", "INTEGER"),
        ("card_click_rate", "REAL"),
        ("last_updated", "TEXT"),
    ],
    pk=["channel_id", "video_id", "date"],
    ts_col="last_updated",
    revisioned=True,
)

# --- Windowed insight tables ------------------------------------------------------
# Aggregate-over-a-window facts (no ``day`` dimension): same shape as video_referrers
# (window_start/window_end in the PK + pulled_at). Pulled by the ``insights`` command,
# not the daily pull. See INSIGHTS_PLAN.md (W1–W4, W6).

VIDEO_RETENTION = TableSpec(
    name="video_retention",
    columns=[
        ("channel_id", "TEXT"),
        ("video_id", "TEXT"),
        ("audience_type", "TEXT"),
        ("elapsed_ratio", "REAL"),
        ("audience_watch_ratio", "REAL"),
        ("relative_retention_performance", "REAL"),
        ("window_start", "TEXT"),
        ("window_end", "TEXT"),
        ("pulled_at", "TEXT"),
    ],
    pk=["channel_id", "video_id", "audience_type", "elapsed_ratio", "window_start", "window_end"],
    ts_col="pulled_at",
)

CHANNEL_DEMOGRAPHICS = TableSpec(
    name="channel_demographics",
    columns=[
        ("channel_id", "TEXT"),
        ("age_group", "TEXT"),
        ("gender", "TEXT"),
        ("subscribed_status", "TEXT"),
        ("viewer_percentage", "REAL"),
        ("window_start", "TEXT"),
        ("window_end", "TEXT"),
        ("pulled_at", "TEXT"),
    ],
    pk=["channel_id", "age_group", "gender", "subscribed_status", "window_start", "window_end"],
    ts_col="pulled_at",
)

AUDIENCE_GEOGRAPHY = TableSpec(
    name="audience_geography",
    columns=[
        ("channel_id", "TEXT"),
        ("country", "TEXT"),
        ("views", "INTEGER"),
        ("estimated_minutes_watched", "INTEGER"),
        ("average_view_percentage", "REAL"),
        ("window_start", "TEXT"),
        ("window_end", "TEXT"),
        ("pulled_at", "TEXT"),
    ],
    pk=["channel_id", "country", "window_start", "window_end"],
    ts_col="pulled_at",
)

AUDIENCE_DEVICES = TableSpec(
    name="audience_devices",
    columns=[
        ("channel_id", "TEXT"),
        ("device_type", "TEXT"),
        ("operating_system", "TEXT"),
        ("views", "INTEGER"),
        ("estimated_minutes_watched", "INTEGER"),
        ("window_start", "TEXT"),
        ("window_end", "TEXT"),
        ("pulled_at", "TEXT"),
    ],
    pk=["channel_id", "device_type", "operating_system", "window_start", "window_end"],
    ts_col="pulled_at",
)

TRAFFIC_SOURCE_DETAIL = TableSpec(
    name="traffic_source_detail",
    columns=[
        ("channel_id", "TEXT"),
        ("video_id", "TEXT"),
        ("traffic_source_type", "TEXT"),
        ("detail", "TEXT"),
        ("views", "INTEGER"),
        ("estimated_minutes_watched", "INTEGER"),
        ("window_start", "TEXT"),
        ("window_end", "TEXT"),
        ("pulled_at", "TEXT"),
    ],
    pk=["channel_id", "video_id", "traffic_source_type", "detail", "window_start", "window_end"],
    ts_col="pulled_at",
)

# --- Subscribed-status daily ------------------------------------------------------
# subscribedStatus composes with ``day`` (unlike the windowed insights), so this rides
# the normal daily pull. See INSIGHTS_PLAN.md (W5).

SUBSCRIBED_STATUS_DAILY = TableSpec(
    name="subscribed_status_daily",
    columns=[
        ("channel_id", "TEXT"),
        ("date", "TEXT"),
        ("subscribed_status", "TEXT"),
        ("views", "INTEGER"),
        ("estimated_minutes_watched", "INTEGER"),
        ("last_updated", "TEXT"),
    ],
    pk=["channel_id", "date", "subscribed_status"],
    ts_col="last_updated",
    revisioned=True,
)

# --- User-maintained dimension ----------------------------------------------------
# Hand-tagged topics per video. Never written by the pull — only by the owner.

VIDEO_TOPICS = TableSpec(
    name="video_topics",
    columns=[
        ("video_id", "TEXT"),
        ("topic", "TEXT"),
        ("added_at", "TEXT"),
    ],
    pk=["video_id", "topic"],
    ts_col="added_at",
)

# Order matters for creation (dims first is not required with IF NOT EXISTS, but tidy).
UPSERT_TABLES: list[TableSpec] = [
    CHANNELS,
    VIDEOS,
    CHANNEL_DAILY,
    CHANNEL_DISCOVERY_DAILY,
    VIDEO_DAILY,
    CHANNEL_TRAFFIC_SOURCES_DAILY,
    VIDEO_REFERRERS,
    CHANNEL_REVENUE_DAILY,
    VIDEO_REVENUE_DAILY,
    VIDEO_TRAFFIC_SOURCES_DAILY,
    VIDEO_DISCOVERY_DAILY,
    VIDEO_RETENTION,
    CHANNEL_DEMOGRAPHICS,
    AUDIENCE_GEOGRAPHY,
    AUDIENCE_DEVICES,
    TRAFFIC_SOURCE_DETAIL,
    SUBSCRIBED_STATUS_DAILY,
    VIDEO_TOPICS,
]

# Secondary indexes for the video-grain tables (they get large; the channel-grain
# tables are small enough not to need them). Created by migrations._create_all.
INDEXES: list[str] = [
    "CREATE INDEX IF NOT EXISTS ix_video_daily_date ON video_daily(date);",
    "CREATE INDEX IF NOT EXISTS ix_video_daily_video ON video_daily(video_id);",
    "CREATE INDEX IF NOT EXISTS ix_video_revenue_daily_date ON video_revenue_daily(date);",
    "CREATE INDEX IF NOT EXISTS ix_video_revenue_daily_video ON video_revenue_daily(video_id);",
    "CREATE INDEX IF NOT EXISTS ix_video_traffic_sources_daily_date "
    "ON video_traffic_sources_daily(date);",
    "CREATE INDEX IF NOT EXISTS ix_video_traffic_sources_daily_video "
    "ON video_traffic_sources_daily(video_id);",
    "CREATE INDEX IF NOT EXISTS ix_video_discovery_daily_date ON video_discovery_daily(date);",
    "CREATE INDEX IF NOT EXISTS ix_video_discovery_daily_video "
    "ON video_discovery_daily(video_id);",
    "CREATE INDEX IF NOT EXISTS ix_video_retention_video ON video_retention(video_id);",
    "CREATE INDEX IF NOT EXISTS ix_traffic_source_detail_video "
    "ON traffic_source_detail(video_id);",
    "CREATE INDEX IF NOT EXISTS ix_subscribed_status_daily_date "
    "ON subscribed_status_daily(date);",
]

TABLES_BY_NAME: dict[str, TableSpec] = {t.name: t for t in UPSERT_TABLES}

# Windowed insight snapshots pulled by `ytmetrics insights` on a cadence. Each run appends
# a whole new snapshot (keyed by window_start/window_end), so these accumulate per-run and
# are subject to the `insights_retention_weeks` prune. NOTE: `video_referrers` is windowed
# too but is an on-demand, one-off attribution pull — deliberately NOT pruned here.
INSIGHT_SNAPSHOT_TABLES: list[str] = [
    "video_retention",
    "channel_demographics",
    "audience_geography",
    "audience_devices",
    "traffic_source_detail",
]

# --- Non-upsert tables ------------------------------------------------------------

META_DDL = "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);"

REVISION_LOG_DDL = """
CREATE TABLE IF NOT EXISTS revision_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  table_name TEXT NOT NULL,
  row_key TEXT NOT NULL,
  column_name TEXT NOT NULL,
  old_value TEXT,
  new_value TEXT,
  pulled_at TEXT NOT NULL
);
"""

# --- Analysis views (stable SQL surfaces for ad-hoc analysis) ---------------------

VIEWS: dict[str, str] = {
    "v_channel_daily_totals": """
        CREATE VIEW v_channel_daily_totals AS
        SELECT
          channel_id,
          date,
          SUM(views)                          AS views,
          SUM(engaged_views)                  AS engaged_views,
          SUM(red_views)                      AS red_views,
          SUM(estimated_minutes_watched)      AS estimated_minutes_watched,
          SUM(estimated_red_minutes_watched)  AS estimated_red_minutes_watched,
          SUM(subscribers_gained)             AS subscribers_gained,
          SUM(subscribers_lost)               AS subscribers_lost,
          SUM(subscribers_gained) - SUM(subscribers_lost) AS net_subscribers,
          SUM(likes)    AS likes,
          SUM(dislikes) AS dislikes,
          SUM(comments) AS comments,
          SUM(shares)   AS shares
        FROM channel_daily
        GROUP BY channel_id, date;
    """,
    "v_shorts_vs_long": """
        CREATE VIEW v_shorts_vs_long AS
        SELECT
          channel_id,
          date,
          CASE WHEN creator_content_type = 'SHORTS' THEN 'shorts' ELSE 'long_form' END AS bucket,
          SUM(views)                     AS views,
          SUM(engaged_views)             AS engaged_views,
          SUM(estimated_minutes_watched) AS estimated_minutes_watched,
          SUM(subscribers_gained) - SUM(subscribers_lost) AS net_subscribers
        FROM channel_daily
        GROUP BY channel_id, date, bucket;
    """,
    "v_video_lifetime": """
        CREATE VIEW v_video_lifetime AS
        SELECT
          vd.channel_id,
          vd.video_id,
          v.title,
          v.content_type,
          v.published_at,
          MIN(vd.date) AS first_date,
          MAX(vd.date) AS last_date,
          SUM(vd.views)                     AS views,
          SUM(vd.engaged_views)             AS engaged_views,
          SUM(vd.estimated_minutes_watched) AS estimated_minutes_watched,
          SUM(vd.likes)                     AS likes,
          SUM(vd.comments)                  AS comments,
          SUM(vd.subscribers_gained)        AS subscribers_gained
        FROM video_daily vd
        LEFT JOIN videos v ON v.video_id = vd.video_id
        GROUP BY vd.channel_id, vd.video_id;
    """,
    "v_view_monetization": """
        CREATE VIEW v_view_monetization AS
        SELECT
          t.channel_id,
          t.date,
          t.views,
          t.red_views                                    AS premium_views,
          r.monetized_playbacks                          AS ad_monetized_playbacks,
          r.ad_impressions                               AS ad_impressions,
          -- approximate: views and playbacks are different units, so this is a proxy.
          t.views - COALESCE(r.monetized_playbacks, 0) - COALESCE(t.red_views, 0)
                                                         AS approx_non_monetized_views
        FROM v_channel_daily_totals t
        LEFT JOIN channel_revenue_daily r
          ON r.channel_id = t.channel_id AND r.date = t.date;
    """,
    "v_paid_vs_organic": """
        CREATE VIEW v_paid_vs_organic AS
        SELECT
          channel_id,
          date,
          CASE WHEN traffic_source_type IN ('ADVERTISING', 'PROMOTED')
               THEN 'paid' ELSE 'organic' END AS bucket,
          SUM(views)                     AS views,
          SUM(estimated_minutes_watched) AS estimated_minutes_watched
        FROM channel_traffic_sources_daily
        GROUP BY channel_id, date, bucket;
    """,
    "v_video_revenue_lifetime": """
        CREATE VIEW v_video_revenue_lifetime AS
        SELECT
          vr.channel_id,
          vr.video_id,
          v.title,
          v.content_type,
          MIN(vr.date) AS first_date,
          MAX(vr.date) AS last_date,
          SUM(vr.estimated_revenue)             AS estimated_revenue,
          SUM(vr.estimated_ad_revenue)          AS estimated_ad_revenue,
          SUM(vr.estimated_red_partner_revenue) AS estimated_red_partner_revenue,
          SUM(vr.gross_revenue)                 AS gross_revenue,
          SUM(vr.monetized_playbacks)           AS monetized_playbacks,
          SUM(vr.ad_impressions)                AS ad_impressions
        FROM video_revenue_daily vr
        LEFT JOIN videos v ON v.video_id = vr.video_id
        GROUP BY vr.channel_id, vr.video_id;
    """,
    "v_revenue_attribution": """
        CREATE VIEW v_revenue_attribution AS
        SELECT
          c.channel_id,
          c.date,
          c.estimated_revenue                          AS channel_revenue,
          COALESCE(v.attributed_revenue, 0)            AS attributed_revenue,
          c.estimated_revenue - COALESCE(v.attributed_revenue, 0)
                                                       AS unattributed_revenue
        FROM channel_revenue_daily c
        LEFT JOIN (
          SELECT channel_id, date, SUM(estimated_revenue) AS attributed_revenue
          FROM video_revenue_daily
          GROUP BY channel_id, date
        ) v ON v.channel_id = c.channel_id AND v.date = c.date;
    """,
}
