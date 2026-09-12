"""Compute and persist transit_line_score for every active listing.

Fetches the IDF Mobilités stop list once, then scores every listing with
known coordinates. Designed to run periodically (stops rarely change;
listings do) -- see infra/cron for scheduling.
"""
from __future__ import annotations

import logging

from pipeline.scrapers.base import get_db_connection
from pipeline.scoring.transit_lines import compute_line_scores
from pipeline.scoring.transit_property import fetch_all_stops, compute_transit_line_score

logger = logging.getLogger(__name__)

UPSERT_SQL = """
INSERT INTO listing_scores (listing_id, transit_line_score, computed_at)
VALUES (%(id)s, %(transit_line_score)s, now())
ON CONFLICT (listing_id) DO UPDATE SET
    transit_line_score = EXCLUDED.transit_line_score,
    computed_at = now();
"""


def run() -> dict:
    conn = get_db_connection()
    try:
        line_scores = compute_line_scores()
        stops = fetch_all_stops()
        logger.info("loaded %s transit stops", len(stops))

        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, ST_Y(geom::geometry) as lat, ST_X(geom::geometry) as lon
                FROM listings
                WHERE is_active = true AND geom IS NOT NULL
                """
            )
            rows = cur.fetchall()

        updates = []
        for listing_id, lat, lon in rows:
            result = compute_transit_line_score(lat, lon, stops, line_scores)
            updates.append({"id": listing_id, "transit_line_score": result["transit_line_score"]})

        if updates:
            with conn.cursor() as cur:
                from psycopg2.extras import execute_batch

                execute_batch(cur, UPSERT_SQL, updates, page_size=200)
            conn.commit()

        return {"stops_loaded": len(stops), "listings_scored": len(updates)}
    finally:
        conn.close()


if __name__ == "__main__":
    import json

    print(json.dumps(run(), indent=2))
