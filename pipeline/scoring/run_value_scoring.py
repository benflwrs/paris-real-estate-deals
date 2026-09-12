"""End-to-end scoring run: fit the value model on DVF, score all active
listings, and persist results to listing_scores.

Run after scrapers + DVF import have populated the DB. Designed to be
invoked by cron alongside pipeline.run_all.
"""
from __future__ import annotations

import logging

import pandas as pd

from pipeline.scrapers.base import get_db_connection
from pipeline.scoring.value import fit_value_model, compute_value_scores

logger = logging.getLogger(__name__)

UPSERT_SCORE_SQL = """
INSERT INTO listing_scores (listing_id, value_score, computed_at)
VALUES (%(id)s, %(value_score)s, now())
ON CONFLICT (listing_id) DO UPDATE SET
    value_score = EXCLUDED.value_score,
    computed_at = now();
"""


def run(insee_prefix: str = "75") -> dict:
    """Fit the value model on DVF transactions matching insee_prefix and
    score all active listings in the same area. Returns summary stats."""
    conn = get_db_connection()
    try:
        dvf_df = pd.read_sql(
            "SELECT price, surface_m2, rooms, insee_code FROM dvf_transactions WHERE insee_code LIKE %(prefix)s",
            conn,
            params={"prefix": f"{insee_prefix}%"},
        )
        if len(dvf_df) < 50:
            logger.warning("too few DVF rows (%s) to fit a reliable model for prefix=%s", len(dvf_df), insee_prefix)
            return {"dvf_rows": len(dvf_df), "scored": 0}

        model = fit_value_model(dvf_df)

        listings_df = pd.read_sql(
            """
            SELECT id, price, surface_m2, rooms, insee_code FROM listings
            WHERE is_active = true AND price IS NOT NULL AND surface_m2 IS NOT NULL
              AND insee_code IS NOT NULL AND insee_code LIKE %(prefix)s
            """,
            conn,
            params={"prefix": f"{insee_prefix}%"},
        )
        if listings_df.empty:
            return {"dvf_rows": len(dvf_df), "scored": 0}

        listings_df["value_score"] = compute_value_scores(listings_df, model)

        with conn.cursor() as cur:
            from psycopg2.extras import execute_batch

            records = listings_df[["id", "value_score"]].to_dict("records")
            execute_batch(cur, UPSERT_SCORE_SQL, records, page_size=200)
        conn.commit()

        return {
            "dvf_rows": len(dvf_df),
            "scored": len(listings_df),
            "median_value_score": float(listings_df["value_score"].median()),
        }
    finally:
        conn.close()


if __name__ == "__main__":
    import argparse
    import json

    parser = argparse.ArgumentParser()
    parser.add_argument("--insee-prefix", default="75")
    args = parser.parse_args()

    print(json.dumps(run(args.insee_prefix), indent=2))
