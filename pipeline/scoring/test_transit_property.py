import pytest

from pipeline.scoring.transit_property import (
    TransitStop,
    haversine_m,
    walk_minutes,
    compute_transit_line_score,
)


def test_haversine_zero_distance():
    assert haversine_m(48.85, 2.35, 48.85, 2.35) == pytest.approx(0, abs=1e-6)


def test_haversine_known_distance():
    # Roughly 111km per degree of latitude at the equator-ish scale;
    # 0.01 deg lat ~= 1.11km
    d = haversine_m(48.85, 2.35, 48.86, 2.35)
    assert 1000 < d < 1200


def test_walk_minutes_conversion():
    # 900m at 75m/min = 12 minutes
    assert walk_minutes(900) == pytest.approx(12.0)


def test_full_credit_within_12_minutes():
    stops = [TransitStop(mode="metro", line_id="1", stop_name="Test", lat=48.85, lon=2.35)]
    line_scores = {"metro:1": 100.0}
    # ~600m away = 8 min walk, well within full-credit window
    result = compute_transit_line_score(48.85, 2.3555, stops, line_scores)
    assert result["transit_line_score"] == pytest.approx(100.0, abs=1)
    assert result["breakdown"][0]["credit"] == "full"


def test_half_credit_between_12_and_20_minutes():
    stops = [TransitStop(mode="metro", line_id="1", stop_name="Test", lat=48.85, lon=2.35)]
    line_scores = {"metro:1": 100.0}
    # ~1200m away = 16 min walk -> half credit
    result = compute_transit_line_score(48.85, 2.3664, stops, line_scores)
    assert result["transit_line_score"] == pytest.approx(50.0, abs=2)
    assert result["breakdown"][0]["credit"] == "half"


def test_no_credit_beyond_20_minutes():
    stops = [TransitStop(mode="metro", line_id="1", stop_name="Test", lat=48.85, lon=2.35)]
    line_scores = {"metro:1": 100.0}
    # ~2000m away = ~27 min walk -> no credit
    result = compute_transit_line_score(48.85, 2.373, stops, line_scores, search_radius_m=3000)
    assert result["transit_line_score"] == 0.0
    assert result["breakdown"] == []


def test_multiple_distinct_lines_all_contribute():
    stops = [
        TransitStop(mode="metro", line_id="1", stop_name="A", lat=48.85, lon=2.35),
        TransitStop(mode="rer", line_id="A", stop_name="B", lat=48.8505, lon=2.3505),
    ]
    line_scores = {"metro:1": 80.0, "rer:A": 90.0}
    result = compute_transit_line_score(48.85, 2.3502, stops, line_scores)
    lines_credited = {b["line"] for b in result["breakdown"]}
    assert lines_credited == {"metro:1", "rer:A"}


def test_same_line_multiple_stops_not_double_counted():
    stops = [
        TransitStop(mode="metro", line_id="1", stop_name="Near", lat=48.85, lon=2.35),
        TransitStop(mode="metro", line_id="1", stop_name="Far", lat=48.86, lon=2.36),
    ]
    line_scores = {"metro:1": 80.0}
    result = compute_transit_line_score(48.85, 2.3505, stops, line_scores)
    assert len(result["breakdown"]) == 1
    assert result["transit_line_score"] == pytest.approx(80.0, abs=1)


def test_unknown_line_id_ignored_gracefully():
    stops = [TransitStop(mode="metro", line_id="99", stop_name="Fake", lat=48.85, lon=2.35)]
    line_scores = {"metro:1": 80.0}  # no entry for metro:99
    result = compute_transit_line_score(48.85, 2.3501, stops, line_scores)
    assert result["transit_line_score"] == 0.0
