"""Persist zone popularity results into zone_scores.

Note: zone_scores.zone_id references a `zones` table keyed by our own
serial id, but we don't yet have IRIS zone polygons loaded (that's a
Phase-2 follow-up -- see plan doc). For now this writes results keyed
directly by insee_code into a lightweight companion table so the data is
usable immediately without blocking on the zones/IRIS geometry work.
"""
from __future__ import annotations

import logging

import pandas as pd

from pipeline.scrapers.base import get_db_connection
from pipeline.scoring.zone_popularity import build_zone_popularity_report

logger = logging.getLogger(__name__)

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS zone_popularity (
    insee_code TEXT NOT NULL,
    transaction_type TEXT NOT NULL,
    median_days_active NUMERIC,
    velocity_score NUMERIC,
    active_count INT,
    delisted_count INT,
    studio_share NUMERIC,
    family_share NUMERIC,
    median_price_per_m2 NUMERIC,
    n_listings INT,
    price_tier TEXT,
    computed_at TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (insee_code, transaction_type)
);
"""

UPSERT_SQL = """
INSERT INTO zone_popularity (
    insee_code, transaction_type, median_days_active, velocity_score,
    active_count, delisted_count, studio_share, family_share,
    median_price_per_m2, n_listings, price_tier, computed_at
) VALUES (
    %(insee_code)s, %(transaction_type)s, %(median_days_active)s, %(velocity_score)s,
    %(active_count)s, %(delisted_count)s, %(studio_share)s, %(family_share)s,
    %(median_price_per_m2)s, %(n_listings)s, %(price_tier)s, now()
)
ON CONFLICT (insee_code, transaction_type) DO UPDATE SET
    median_days_active = EXCLUDED.median_days_active,
    velocity_score = EXCLUDED.velocity_score,
    active_count = EXCLUDED.active_count,
    delisted_count = EXCLUDED.delisted_count,
    studio_share = EXCLUDED.studio_share,
    family_share = EXCLUDED.family_share,
    median_price_per_m2 = EXCLUDED.median_price_per_m2,
    n_listings = EXCLUDED.n_listings,
    price_tier = EXCLUDED.price_tier,
    computed_at = now();
"""


def run() -> dict:
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(CREATE_TABLE_SQL)
        conn.commit()

        listings_df = pd.read_sql(
            """
            SELECT insee_code, transaction_type, surface_m2, rooms, price, price_per_m2,
                   first_seen_at, last_seen_at, is_active
            FROM listings WHERE insee_code IS NOT NULL
            """,
            conn,
        )
        if listings_df.empty:
            return {"zones_scored": 0}

        report = build_zone_popularity_report(listings_df)
        report = report.where(pd.notnull(report), None)

        records = report.to_dict("records")
        for r in records:
            if r.get("price_tier") is not None:
                r["price_tier"] = str(r["price_tier"])

        with conn.cursor() as cur:
            from psycopg2.extras import execute_batch

            execute_batch(cur, UPSERT_SQL, records, page_size=200)
        conn.commit()

        return {"zones_scored": len(records)}
    finally:
        conn.close()


if __name__ == "__main__":
    import json

    print(json.dumps(run(), indent=2))
