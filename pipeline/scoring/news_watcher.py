"""News watcher: rule-based signal for upcoming projects affecting prices.

Ben's brief: no LLM needed for this -- keyword/RSS scan of official sources
+ geocoding is enough for the well-known project categories (new metro
stations, urban renewal, etc.). This module implements exactly that.

Sources (all free, no auth):
  - Le Journal du Grand Paris RSS feed: dedicated coverage of Grand Paris
    Express construction/openings, urban development projects, transport
    investment -- exactly the beat Ben described.
  - Google News RSS search (no API key, standard `news.google.com/rss/search`
    endpoint) for broader keyword coverage of announcements this single
    source might miss.

Pipeline: fetch RSS -> keyword match against a curated French keyword list
-> for matched articles, try to extract a place name and geocode it via the
free BAN (Base Adresse Nationale) API -> store as a news_signal row with an
impact_weight that decays with age.

Deliberately NOT using an LLM: keyword matching + geocoding is fully
deterministic, free, and sufficient for the concrete signal categories
requested (new stations, line extensions, urban renewal, ZAC). A hook is
left (`classify_article`) for a future LLM upgrade if the keyword approach
proves too noisy/sparse once running for a while -- but ship without it.
"""
from __future__ import annotations

import datetime
import logging
import re
from dataclasses import dataclass, field
from typing import Optional
from xml.etree import ElementTree

import httpx

logger = logging.getLogger(__name__)

RSS_SOURCES = {
    "lejournaldugrandparis": "https://www.lejournaldugrandparis.fr/feed/",
}

GOOGLE_NEWS_SEARCH_TMPL = (
    "https://news.google.com/rss/search?q={query}&hl=fr&gl=FR&ceid=FR:fr"
)
GOOGLE_NEWS_QUERIES = [
    "Grand Paris Express nouvelle station",
    "extension ligne métro RER Île-de-France",
    "rénovation urbaine ZAC Paris",
]

# Keywords signalling a project likely to move nearby real-estate prices.
# Grouped by rough impact category so different weights can be applied
# later if useful; kept flat for the v1 keyword match.
IMPACT_KEYWORDS = [
    "nouvelle station", "mise en service", "extension de ligne",
    "prolongement de la ligne", "rénovation urbaine", "ZAC",
    "zone d'aménagement concerté", "quartier prioritaire",
    "nouveau quartier", "aménagement urbain", "grand paris express",
    "nouvelle gare", "réaménagement", "requalification urbaine",
    "programme immobilier", "écoquartier", "renouvellement urbain",
]

# Very rough decay: a signal from today counts fully; by DECAY_HORIZON_DAYS
# it has decayed to near-zero relevance for "upcoming" framing.
DECAY_HORIZON_DAYS = 730  # ~2 years -- construction/opening news stays
                           # relevant for a while, unlike day-to-day news


@dataclass
class NewsArticle:
    source: str
    title: str
    url: str
    published_at: Optional[datetime.datetime]
    raw_text: str
    matched_keywords: list[str] = field(default_factory=list)


def fetch_rss(url: str, client: Optional[httpx.Client] = None) -> list[dict]:
    """Minimal RSS parser (stdlib xml, no feedparser dependency) -- RSS 2.0
    is simple enough that a full feed library isn't warranted here."""
    own_client = client is None
    client = client or httpx.Client(timeout=20, headers={"User-Agent": "Mozilla/5.0"})
    try:
        resp = client.get(url)
        resp.raise_for_status()
        root = ElementTree.fromstring(resp.content)
        items = []
        for item in root.findall(".//item"):
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            pub_date_raw = item.findtext("pubDate")
            description = (item.findtext("description") or "").strip()
            items.append({"title": title, "link": link, "pub_date_raw": pub_date_raw, "description": description})
        return items
    except (httpx.HTTPError, ElementTree.ParseError) as exc:
        logger.warning("failed to fetch/parse RSS %s: %s", url, exc)
        return []
    finally:
        if own_client:
            client.close()


def parse_rss_date(raw: Optional[str]) -> Optional[datetime.datetime]:
    if not raw:
        return None
    from email.utils import parsedate_to_datetime

    try:
        return parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None


def match_keywords(text: str) -> list[str]:
    text_lower = text.lower()
    return [kw for kw in IMPACT_KEYWORDS if kw in text_lower]


def classify_article(article: NewsArticle) -> list[str]:
    """Extension point: currently just returns matched keywords. Replace
    or augment with an LLM call here later if keyword matching proves too
    brittle for a broader class of articles -- kept isolated so that swap
    doesn't touch the rest of the pipeline."""
    return article.matched_keywords


def collect_articles(client: Optional[httpx.Client] = None) -> list[NewsArticle]:
    """Fetch all configured sources and return only articles matching at
    least one impact keyword."""
    own_client = client is None
    client = client or httpx.Client(timeout=20, headers={"User-Agent": "Mozilla/5.0"})
    articles: list[NewsArticle] = []
    try:
        for source_name, url in RSS_SOURCES.items():
            for item in fetch_rss(url, client=client):
                text = f"{item['title']} {item['description']}"
                keywords = match_keywords(text)
                if not keywords:
                    continue
                articles.append(
                    NewsArticle(
                        source=source_name,
                        title=item["title"],
                        url=item["link"],
                        published_at=parse_rss_date(item["pub_date_raw"]),
                        raw_text=text,
                        matched_keywords=keywords,
                    )
                )

        for query in GOOGLE_NEWS_QUERIES:
            import urllib.parse

            url = GOOGLE_NEWS_SEARCH_TMPL.format(query=urllib.parse.quote(query))
            for item in fetch_rss(url, client=client):
                text = f"{item['title']} {item['description']}"
                keywords = match_keywords(text)
                if not keywords:
                    continue
                articles.append(
                    NewsArticle(
                        source="google_news",
                        title=item["title"],
                        url=item["link"],
                        published_at=parse_rss_date(item["pub_date_raw"]),
                        raw_text=text,
                        matched_keywords=keywords,
                    )
                )
        return articles
    finally:
        if own_client:
            client.close()


# --- Place extraction + geocoding ---

# Very lightweight place-name extraction: look for capitalized multi-word
# sequences near common location cue words. This is intentionally simple
# (a real NER model would do better but isn't needed here -- most of these
# headlines name a station/commune explicitly and BAN's own geocoder is
# forgiving of imperfect queries) -- fall back to geocoding the whole
# title if no cue-based extraction succeeds.
LOCATION_CUE_PATTERN = re.compile(
    r"(?:gare|station|quartier|ligne|à|de|d')\s+([A-ZÀ-Ý][\wÀ-ÿ\'\-]+(?:\s+[A-ZÀ-Ý][\wÀ-ÿ\'\-]+){0,3})"
)


def extract_place_candidates(text: str) -> list[str]:
    matches = LOCATION_CUE_PATTERN.findall(text)
    # de-dup while preserving order
    seen = set()
    out = []
    for m in matches:
        if m not in seen:
            seen.add(m)
            out.append(m)
    return out


def geocode_place(place: str, client: Optional[httpx.Client] = None) -> Optional[dict]:
    """Geocode via the free BAN API. Returns {lat, lon, insee_code, label,
    score} or None if no confident match (score < 0.4, per BAN's own
    documented reliability threshold for real French addresses).

    Biases results toward Paris/Ile-de-France (lat/lon proximity params) --
    without this, a same-named commune elsewhere in France (e.g. there is
    a "Massy" in Guadeloupe, a "Clamart" in Brittany) can outrank the
    intended Ile-de-France result. Confirmed via live testing: unbiased
    queries for "Massy" and "Clamart" returned the wrong region entirely.
    """
    own_client = client is None
    client = client or httpx.Client(timeout=15)
    try:
        resp = client.get(
            "https://api-adresse.data.gouv.fr/search/",
            params={"q": place, "limit": 1, "lat": 48.8566, "lon": 2.3522},
        )
        resp.raise_for_status()
        data = resp.json()
        features = data.get("features", [])
        if not features:
            return None
        feature = features[0]
        props = feature.get("properties", {})
        score = props.get("score", 0)
        if score < 0.4:
            return None
        lon, lat = feature["geometry"]["coordinates"]
        # Defensive check: even with proximity bias, reject a result that
        # lands clearly outside Ile-de-France (rough bounding box) rather
        # than silently accepting a wrong-region match.
        if not (1.4 <= lon <= 3.6 and 48.1 <= lat <= 49.3):
            logger.warning("geocode for %r landed outside Ile-de-France (%s, %s) -- rejecting", place, lat, lon)
            return None
        return {
            "lat": lat,
            "lon": lon,
            "insee_code": props.get("citycode"),
            "label": props.get("label"),
            "score": score,
        }
    except httpx.HTTPError as exc:
        logger.warning("geocoding failed for %r: %s", place, exc)
        return None
    finally:
        if own_client:
            client.close()


def impact_weight_for_age(published_at: Optional[datetime.datetime], as_of: Optional[datetime.date] = None) -> float:
    """Linear decay from 1.0 (published today) to ~0.1 at DECAY_HORIZON_DAYS,
    floored at 0.1 so old-but-still-relevant construction news isn't
    entirely zeroed out."""
    if published_at is None:
        return 0.5  # unknown date -- assume moderate relevance
    as_of = as_of or datetime.date.today()
    pub_date = published_at.date() if hasattr(published_at, "date") else published_at
    age_days = max((as_of - pub_date).days, 0)
    decay = max(1.0 - (age_days / DECAY_HORIZON_DAYS), 0.1)
    return round(decay, 3)
