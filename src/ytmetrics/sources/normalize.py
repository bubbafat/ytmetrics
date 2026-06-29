"""Turn raw YouTube Analytics ``reports.query`` responses into normalized store rows.

A response looks like::

    {"columnHeaders": [{"name": "day"}, {"name": "views"}, ...],
     "rows": [["2026-06-01", 100, ...], ...]}

The same shaping runs for LiveSource (responses from the API) and ReplaySource
(responses recorded as fixtures), so normalization itself is covered by offline tests.
"""

from __future__ import annotations

from typing import Any

# YouTube API dimension/metric name -> our snake_case column name.
API_TO_COL: dict[str, str] = {
    # dimensions
    "day": "date",
    "creatorContentType": "creator_content_type",
    "insightTrafficSourceType": "traffic_source_type",
    "insightTrafficSourceDetail": "referrer_detail",
    "video": "video_id",
    # audience / insight dimensions
    "ageGroup": "age_group",
    "gender": "gender",
    "country": "country",
    "deviceType": "device_type",
    "operatingSystem": "operating_system",
    "elapsedVideoTimeRatio": "elapsed_ratio",
    "audienceType": "audience_type",
    "subscribedStatus": "subscribed_status",
    # activity metrics
    "views": "views",
    "engagedViews": "engaged_views",
    "redViews": "red_views",
    "estimatedMinutesWatched": "estimated_minutes_watched",
    "estimatedRedMinutesWatched": "estimated_red_minutes_watched",
    "averageViewDuration": "average_view_duration",
    "averageViewPercentage": "average_view_percentage",
    "subscribersGained": "subscribers_gained",
    "subscribersLost": "subscribers_lost",
    "likes": "likes",
    "dislikes": "dislikes",
    "comments": "comments",
    "shares": "shares",
    # discovery metrics
    "videoThumbnailImpressions": "video_thumbnail_impressions",
    "videoThumbnailImpressionsClickRate": "video_thumbnail_impressions_click_rate",
    "cardImpressions": "card_impressions",
    "cardClickRate": "card_click_rate",
    # revenue metrics
    "estimatedRevenue": "estimated_revenue",
    "estimatedAdRevenue": "estimated_ad_revenue",
    "estimatedRedPartnerRevenue": "estimated_red_partner_revenue",
    "grossRevenue": "gross_revenue",
    "cpm": "cpm",
    "playbackBasedCpm": "playback_based_cpm",
    "monetizedPlaybacks": "monetized_playbacks",
    "adImpressions": "ad_impressions",
    # audience / insight metrics
    "viewerPercentage": "viewer_percentage",
    "audienceWatchRatio": "audience_watch_ratio",
    "relativeRetentionPerformance": "relative_retention_performance",
}

# insightTrafficSourceDetail maps to ``referrer_detail`` for video_referrers; the
# traffic_source_detail table wants the column named ``detail``. This per-call override
# is applied by the dedicated builder below (the global map is left unchanged).
_DETAIL_OVERRIDE = {"insightTrafficSourceDetail": "detail"}


def normalize_rows(resp: dict[str, Any], extra: dict[str, Any] | None = None) -> list[dict]:
    """Map each response row to a dict of {our_column: value}, merging ``extra``."""
    headers = [h["name"] for h in resp.get("columnHeaders", [])]
    cols = [API_TO_COL.get(h, h) for h in headers]
    out: list[dict[str, Any]] = []
    for raw in resp.get("rows", []) or []:
        rec: dict[str, Any] = dict(zip(cols, raw, strict=True))
        if extra:
            rec.update(extra)
        out.append(rec)
    return out


# The API returns creatorContentType in camelCase ("shorts", "videoOnDemand", …);
# canonicalize to the documented UPPER_SNAKE enum so stored data and views agree.
_CONTENT_TYPE_CANON = {
    "shorts": "SHORTS",
    "videoOnDemand": "VIDEO_ON_DEMAND",
    "liveStream": "LIVE_STREAM",
    "story": "STORY",
    "creatorContentTypeUnspecified": "UNSPECIFIED",
}


def canon_content_type(value: Any) -> Any:
    if value is None:
        return None
    return _CONTENT_TYPE_CANON.get(value, value)


def channel_daily_rows(resp: dict[str, Any], channel_id: str) -> list[dict]:
    rows = normalize_rows(resp, {"channel_id": channel_id})
    for r in rows:
        ct = canon_content_type(r.get("creator_content_type"))
        r["creator_content_type"] = ct or "UNSPECIFIED"
    return rows


def traffic_sources_rows(resp: dict[str, Any], channel_id: str) -> list[dict]:
    return normalize_rows(resp, {"channel_id": channel_id})


def discovery_rows(resp: dict[str, Any], channel_id: str) -> list[dict]:
    return normalize_rows(resp, {"channel_id": channel_id})


def revenue_rows(resp: dict[str, Any], channel_id: str) -> list[dict]:
    return normalize_rows(resp, {"channel_id": channel_id})


def video_revenue_rows(resp: dict[str, Any], channel_id: str) -> list[dict]:
    # ``video`` is already mapped to video_id in API_TO_COL.
    return normalize_rows(resp, {"channel_id": channel_id})


def video_traffic_sources_rows(resp: dict[str, Any], channel_id: str) -> list[dict]:
    return normalize_rows(resp, {"channel_id": channel_id})


def video_discovery_rows(resp: dict[str, Any], channel_id: str) -> list[dict]:
    return normalize_rows(resp, {"channel_id": channel_id})


def video_daily_rows(resp: dict[str, Any], channel_id: str) -> tuple[list[dict], dict[str, str]]:
    """Return (video_daily rows, {video_id: content_type}).

    The API dimension is ``creatorContentType``; in video_daily we store it as
    ``content_type`` and also surface it so the videos dim can be tagged.
    """
    rows = normalize_rows(resp, {"channel_id": channel_id})
    content_types: dict[str, str] = {}
    for r in rows:
        ct = canon_content_type(r.pop("creator_content_type", None)) or "UNSPECIFIED"
        r["content_type"] = ct
        vid = r.get("video_id")
        if vid:
            content_types[vid] = ct
    return rows, content_types


def referrer_rows(
    resp: dict[str, Any],
    channel_id: str,
    dest_video_id: str,
    traffic_source_type: str,
    window_start: str,
    window_end: str,
) -> list[dict]:
    rows = normalize_rows(
        resp,
        {
            "channel_id": channel_id,
            "dest_video_id": dest_video_id,
            "traffic_source_type": traffic_source_type,
            "window_start": window_start,
            "window_end": window_end,
        },
    )
    for r in rows:
        # For video-referring source types the detail IS a video id.
        r["referrer_video_id"] = r.get("referrer_detail")
    return rows


# --- Windowed insight builders (W1–W4, W6) ---------------------------------------
# All carry channel_id + window_start/window_end; most are plain ``normalize_rows``.


def retention_rows(
    resp: dict[str, Any],
    channel_id: str,
    video_id: str,
    audience_type: str,
    window_start: str,
    window_end: str,
) -> list[dict]:
    return normalize_rows(
        resp,
        {
            "channel_id": channel_id,
            "video_id": video_id,
            "audience_type": audience_type,
            "window_start": window_start,
            "window_end": window_end,
        },
    )


def demographics_rows(
    resp: dict[str, Any], channel_id: str, window_start: str, window_end: str
) -> list[dict]:
    # v1 query is ageGroup,gender only; store subscribed_status='ALL' so the PK is stable.
    return normalize_rows(
        resp,
        {
            "channel_id": channel_id,
            "subscribed_status": "ALL",
            "window_start": window_start,
            "window_end": window_end,
        },
    )


def geography_rows(
    resp: dict[str, Any], channel_id: str, window_start: str, window_end: str
) -> list[dict]:
    return normalize_rows(
        resp,
        {"channel_id": channel_id, "window_start": window_start, "window_end": window_end},
    )


def devices_rows(
    resp: dict[str, Any], channel_id: str, window_start: str, window_end: str
) -> list[dict]:
    return normalize_rows(
        resp,
        {"channel_id": channel_id, "window_start": window_start, "window_end": window_end},
    )


def traffic_source_detail_rows(
    resp: dict[str, Any],
    channel_id: str,
    video_id: str,
    traffic_source_type: str,
    window_start: str,
    window_end: str,
) -> list[dict]:
    """Like normalize_rows but maps insightTrafficSourceDetail -> ``detail`` (not
    ``referrer_detail``), for the generalized traffic_source_detail table."""
    headers = [h["name"] for h in resp.get("columnHeaders", [])]
    cols = [_DETAIL_OVERRIDE.get(h, API_TO_COL.get(h, h)) for h in headers]
    extra = {
        "channel_id": channel_id,
        "video_id": video_id,
        "traffic_source_type": traffic_source_type,
        "window_start": window_start,
        "window_end": window_end,
    }
    out: list[dict[str, Any]] = []
    for raw in resp.get("rows", []) or []:
        rec: dict[str, Any] = dict(zip(cols, raw, strict=True))
        rec.update(extra)
        out.append(rec)
    return out


def subscribed_status_rows(resp: dict[str, Any], channel_id: str) -> list[dict]:
    return normalize_rows(resp, {"channel_id": channel_id})
