from __future__ import annotations

from ytmetrics.sources import normalize


def test_normalize_maps_headers():
    resp = {
        "columnHeaders": [{"name": "day"}, {"name": "creatorContentType"}, {"name": "views"}],
        "rows": [["2026-06-01", "SHORTS", 500]],
    }
    rows = normalize.channel_daily_rows(resp, "UC1")
    assert rows == [
        {"date": "2026-06-01", "creator_content_type": "SHORTS", "views": 500, "channel_id": "UC1"}
    ]


def test_canonicalizes_camelcase_content_type():
    # The live API returns camelCase; we store the documented UPPER_SNAKE enum.
    resp = {
        "columnHeaders": [{"name": "day"}, {"name": "creatorContentType"}, {"name": "views"}],
        "rows": [
            ["2026-06-01", "shorts", 500],
            ["2026-06-01", "videoOnDemand", 100],
            ["2026-06-01", "creatorContentTypeUnspecified", 3],
        ],
    }
    types = [r["creator_content_type"] for r in normalize.channel_daily_rows(resp, "UC1")]
    assert types == ["SHORTS", "VIDEO_ON_DEMAND", "UNSPECIFIED"]


def test_channel_daily_defaults_content_type_when_absent():
    resp = {"columnHeaders": [{"name": "day"}, {"name": "views"}], "rows": [["2026-06-01", 10]]}
    rows = normalize.channel_daily_rows(resp, "UC1")
    assert rows[0]["creator_content_type"] == "UNSPECIFIED"


def test_video_daily_extracts_content_type():
    resp = {
        "columnHeaders": [
            {"name": "video"}, {"name": "day"}, {"name": "creatorContentType"}, {"name": "views"}
        ],
        "rows": [["vid1", "2026-06-01", "VIDEO_ON_DEMAND", 42]],
    }
    rows, cts = normalize.video_daily_rows(resp, "UC1")
    assert rows[0]["content_type"] == "VIDEO_ON_DEMAND"
    assert "creator_content_type" not in rows[0]
    assert cts == {"vid1": "VIDEO_ON_DEMAND"}


def test_referrer_rows_set_video_id():
    resp = {
        "columnHeaders": [{"name": "insightTrafficSourceDetail"}, {"name": "views"}],
        "rows": [["shortABC", 30]],
    }
    rows = normalize.referrer_rows(
        resp, "UC1", "destVID", "RELATED_VIDEO", "2026-06-01", "2026-06-07"
    )
    assert rows[0]["referrer_video_id"] == "shortABC"
    assert rows[0]["dest_video_id"] == "destVID"
    assert rows[0]["traffic_source_type"] == "RELATED_VIDEO"
