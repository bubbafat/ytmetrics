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
}


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
