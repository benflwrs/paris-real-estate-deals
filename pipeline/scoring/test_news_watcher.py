import datetime
from unittest.mock import MagicMock

import pytest

from pipeline.scoring.news_watcher import (
    match_keywords,
    parse_rss_date,
    extract_place_candidates,
    geocode_place,
    impact_weight_for_age,
    fetch_rss,
    NewsArticle,
    classify_article,
)

SAMPLE_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
<title>Test feed</title>
<item>
  <title>Nouvelle station du Grand Paris Express à Créteil l'Échat</title>
  <link>https://example.com/article1</link>
  <pubDate>Fri, 11 Sep 2026 08:29:16 +0000</pubDate>
  <description>Un nouveau quartier de 800 logements prévu près de la future gare.</description>
</item>
<item>
  <title>Un article sans rapport avec les transports</title>
  <link>https://example.com/article2</link>
  <pubDate>Fri, 11 Sep 2026 08:00:00 +0000</pubDate>
  <description>Recette de cuisine du jour.</description>
</item>
</channel></rss>"""


def test_match_keywords_finds_relevant_terms():
    text = "Nouvelle station du Grand Paris Express à Creteil"
    matches = match_keywords(text)
    assert "nouvelle station" in matches
    assert "grand paris express" in matches


def test_match_keywords_empty_for_irrelevant_text():
    assert match_keywords("Recette de cuisine du jour") == []


def test_parse_rss_date_valid():
    dt = parse_rss_date("Fri, 11 Sep 2026 08:29:16 +0000")
    assert dt is not None
    assert dt.year == 2026
    assert dt.month == 9


def test_parse_rss_date_invalid_returns_none():
    assert parse_rss_date("not a date") is None
    assert parse_rss_date(None) is None


def test_fetch_rss_parses_items():
    resp = MagicMock()
    resp.content = SAMPLE_RSS.encode("utf-8")
    resp.raise_for_status.return_value = None
    client = MagicMock()
    client.get.return_value = resp

    items = fetch_rss("http://fake-url", client=client)
    assert len(items) == 2
    assert "Créteil" in items[0]["title"]


def test_fetch_rss_handles_malformed_xml_gracefully():
    resp = MagicMock()
    resp.content = b"<not valid xml"
    resp.raise_for_status.return_value = None
    client = MagicMock()
    client.get.return_value = resp

    items = fetch_rss("http://fake-url", client=client)
    assert items == []


def test_extract_place_candidates_finds_named_locations():
    text = "Nouvelle station du Grand Paris Express à Créteil l'Échat"
    candidates = extract_place_candidates(text)
    assert any("Créteil" in c for c in candidates)


def test_geocode_place_returns_none_for_low_score():
    resp = MagicMock()
    resp.json.return_value = {
        "features": [{"properties": {"score": 0.1, "citycode": "75056"}, "geometry": {"coordinates": [2.35, 48.85]}}]
    }
    resp.raise_for_status.return_value = None
    client = MagicMock()
    client.get.return_value = resp

    assert geocode_place("gibberish query", client=client) is None


def test_geocode_place_returns_result_for_confident_match():
    resp = MagicMock()
    resp.json.return_value = {
        "features": [
            {
                "properties": {"score": 0.8, "citycode": "94028", "label": "Créteil"},
                "geometry": {"coordinates": [2.45, 48.79]},
            }
        ]
    }
    resp.raise_for_status.return_value = None
    client = MagicMock()
    client.get.return_value = resp

    result = geocode_place("Créteil", client=client)
    assert result is not None
    assert result["insee_code"] == "94028"
    assert result["lat"] == 48.79


def test_geocode_place_returns_none_for_empty_results():
    resp = MagicMock()
    resp.json.return_value = {"features": []}
    resp.raise_for_status.return_value = None
    client = MagicMock()
    client.get.return_value = resp

    assert geocode_place("nonexistent place xyz", client=client) is None


def test_geocode_place_rejects_result_outside_ile_de_france():
    resp = MagicMock()
    resp.json.return_value = {
        "features": [
            {
                # Guadeloupe coordinates -- same-named commune elsewhere in France
                "properties": {"score": 0.95, "citycode": "97129", "label": "Massy 97115 Sainte-Rose"},
                "geometry": {"coordinates": [-61.7, 16.3]},
            }
        ]
    }
    resp.raise_for_status.return_value = None
    client = MagicMock()
    client.get.return_value = resp

    assert geocode_place("Massy", client=client) is None


def test_impact_weight_full_for_todays_news():
    now = datetime.datetime.now(datetime.timezone.utc)
    weight = impact_weight_for_age(now)
    assert weight == pytest.approx(1.0, abs=0.01)


def test_impact_weight_decays_with_age():
    old_date = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=365)
    weight = impact_weight_for_age(old_date)
    assert 0.1 < weight < 0.9


def test_impact_weight_floors_at_point_one():
    very_old = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=3650)
    weight = impact_weight_for_age(very_old)
    assert weight == 0.1


def test_impact_weight_unknown_date_returns_moderate():
    assert impact_weight_for_age(None) == 0.5


def test_classify_article_returns_matched_keywords():
    article = NewsArticle(
        source="test", title="t", url="u", published_at=None, raw_text="x",
        matched_keywords=["nouvelle station"],
    )
    assert classify_article(article) == ["nouvelle station"]
