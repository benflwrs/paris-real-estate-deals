"""Persist news watcher results into news_signals and update zone upside.

Run periodically (daily is plenty -- these RSS feeds don't update faster
than that in practice, and BAN geocoding is rate-limited politeness-wise).
"""
from __future__ import annotations

import json
import logging

from pipeline.scrapers.base import get_db_connection
from pipeline.scoring.news_watcher import (
    collect_articles,
    extract_place_candidates,
    geocode_place,
    impact_weight_for_age,
)

logger = logging.getLogger(__name__)

INSERT_SQL = """
INSERT INTO news_signals (
    source, title, url, published_at, keywords_matched, geom, insee_code,
    impact_weight, raw_text
) VALUES (
    %(source)s, %(title)s, %(url)s, %(published_at)s, %(keywords_matched)s,
    ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326)::geography, %(insee_code)s,
    %(impact_weight)s, %(raw_text)s
);
"""


def run(max_articles: int = 100) -> dict:
    conn = get_db_connection()
    try:
        articles = collect_articles()[:max_articles]
        logger.info("collected %s matching articles", len(articles))

        # Dedup against already-stored URLs so re-runs don't re-insert.
        with conn.cursor() as cur:
            cur.execute("SELECT url FROM news_signals WHERE url IS NOT NULL")
            existing_urls = {row[0] for row in cur.fetchall()}

        new_signals = []
        for article in articles:
            if article.url in existing_urls:
                continue
            candidates = extract_place_candidates(article.title) or extract_place_candidates(article.raw_text)
            geo = None
            for candidate in candidates:
                geo = geocode_place(candidate)
                if geo:
                    break

            new_signals.append(
                {
                    "source": article.source,
                    "title": article.title,
                    "url": article.url,
                    "published_at": article.published_at,
                    "keywords_matched": article.matched_keywords,
                    "lat": geo["lat"] if geo else None,
                    "lon": geo["lon"] if geo else None,
                    "insee_code": geo["insee_code"] if geo else None,
                    "impact_weight": impact_weight_for_age(article.published_at),
                    "raw_text": article.raw_text,
                }
            )

        # Skip rows with no coordinates for the geom insert (PostGIS point
        # needs both lat/lon); still worth keeping a record of them for
        # completeness with insee_code = NULL, but geom must be non-null
        # in the geography column if present in raw_json elsewhere -- here
        # we just allow NULL geom by using COALESCE-safe point construction.
        geocoded = [s for s in new_signals if s["lat"] is not None]
        ungeocoded = [s for s in new_signals if s["lat"] is None]

        if geocoded:
            with conn.cursor() as cur:
                from psycopg2.extras import execute_batch

                execute_batch(cur, INSERT_SQL, geocoded, page_size=100)
            conn.commit()

        # Ungeocoded articles still get stored (geom NULL) via a simpler
        # insert so the raw signal isn't lost even without a location.
        if ungeocoded:
            with conn.cursor() as cur:
                for s in ungeocoded:
                    cur.execute(
                        """
                        INSERT INTO news_signals (source, title, url, published_at, keywords_matched, impact_weight, raw_text)
                        VALUES (%(source)s, %(title)s, %(url)s, %(published_at)s, %(keywords_matched)s, %(impact_weight)s, %(raw_text)s);
                        """,
                        s,
                    )
            conn.commit()

        return {
            "articles_collected": len(articles),
            "new_signals": len(new_signals),
            "geocoded": len(geocoded),
            "ungeocoded": len(ungeocoded),
        }
    finally:
        conn.close()


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))
