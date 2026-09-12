"""DVF (Demandes de Valeurs Foncieres) importer.

DVF is the official French government dataset of *closed real-estate sale
transactions* (not live listings). It is used exclusively as reference data
to calibrate the "fair value" model in Phase 2 -- it is never surfaced to
the frontend as if it were an active listing.

Source: https://files.data.gouv.fr/geo-dvf/latest/csv/<year>/departements/<dept>.csv.gz
Per-department, per-year files, updated semi-annually. No auth needed.

Ile-de-France departments: 75 (Paris), 77, 78, 91, 92, 93, 94, 95.
"""
from __future__ import annotations

import gzip
import io
import logging
from typing import Iterator, Optional

import httpx
import pandas as pd

from pipeline.scrapers.base import get_db_connection

logger = logging.getLogger(__name__)

IDF_DEPARTMENTS = ["75", "77", "78", "91", "92", "93", "94", "95"]
BASE_URL = "https://files.data.gouv.fr/geo-dvf/latest/csv"

RELEVANT_TYPES = {"Appartement", "Maison"}

INSERT_SQL = """
INSERT INTO dvf_transactions (
    mutation_date, price, surface_m2, rooms, property_type, insee_code, geom
) VALUES (
    %(mutation_date)s, %(price)s, %(surface_m2)s, %(rooms)s, %(property_type)s,
    %(insee_code)s, ST_SetSRID(ST_MakePoint(%(longitude)s, %(latitude)s), 4326)::geography
);
"""


def download_department_csv(year: int, dept: str, client: Optional[httpx.Client] = None) -> bytes:
    """Download and decompress one department/year CSV. Returns raw CSV bytes."""
    own_client = client is None
    client = client or httpx.Client(timeout=60, follow_redirects=True)
    try:
        url = f"{BASE_URL}/{year}/departements/{dept}.csv.gz"
        resp = client.get(url, follow_redirects=True)
        resp.raise_for_status()
        return gzip.decompress(resp.content)
    finally:
        if own_client:
            client.close()


def parse_dvf_csv(csv_bytes: bytes) -> pd.DataFrame:
    """Parse raw DVF CSV bytes into a cleaned DataFrame ready for insertion.

    Filters to Appartement/Maison, drops rows missing price/surface/coords,
    and de-duplicates the DVF quirk where a single mutation can span
    multiple rows (one per lot) by keeping one row per (id_mutation) with
    the *first* Appartement/Maison row's surface (surface_reelle_bati is
    already per-lot-type in this dataset for the typical case).
    """
    df = pd.read_csv(
        io.BytesIO(csv_bytes),
        dtype={"code_postal": str, "code_commune": str},
        low_memory=False,
    )
    df = df[df["type_local"].isin(RELEVANT_TYPES)]
    df = df.dropna(subset=["valeur_fonciere", "surface_reelle_bati", "longitude", "latitude"])
    df = df[df["valeur_fonciere"] > 0]
    df = df[df["surface_reelle_bati"] > 0]

    out = pd.DataFrame(
        {
            "mutation_date": pd.to_datetime(df["date_mutation"]).dt.date,
            "price": df["valeur_fonciere"].astype(float),
            "surface_m2": df["surface_reelle_bati"].astype(float),
            "rooms": df["nombre_pieces_principales"].fillna(0).astype(int),
            "property_type": df["type_local"],
            "insee_code": df["code_commune"],
            "longitude": df["longitude"].astype(float),
            "latitude": df["latitude"].astype(float),
        }
    )
    return out


def import_department_year(year: int, dept: str, conn=None) -> int:
    """Download, parse, and insert one department/year into dvf_transactions.
    Returns number of rows inserted."""
    own_conn = conn is None
    conn = conn or get_db_connection()
    try:
        raw = download_department_csv(year, dept)
        df = parse_dvf_csv(raw)
        records = df.to_dict("records")
        if not records:
            return 0
        with conn.cursor() as cur:
            from psycopg2.extras import execute_batch

            execute_batch(cur, INSERT_SQL, records, page_size=1000)
        conn.commit()
        logger.info("imported %s rows for dept=%s year=%s", len(records), dept, year)
        return len(records)
    finally:
        if own_conn:
            conn.close()


def import_idf(years: Optional[list[int]] = None, departments: Optional[list[str]] = None) -> dict:
    """Import all Ile-de-France departments for the given years (default:
    current + previous year, per DVF's ~1yr lag on the latest data)."""
    import datetime

    years = years or [datetime.date.today().year, datetime.date.today().year - 1]
    departments = departments or IDF_DEPARTMENTS

    conn = get_db_connection()
    results = {}
    try:
        for year in years:
            for dept in departments:
                key = f"{dept}-{year}"
                try:
                    results[key] = import_department_year(year, dept, conn=conn)
                except httpx.HTTPStatusError as exc:
                    logger.warning("skip %s: %s", key, exc)
                    results[key] = 0
        return results
    finally:
        conn.close()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--years", type=int, nargs="+", default=None)
    parser.add_argument("--departments", nargs="+", default=None)
    args = parser.parse_args()

    print(import_idf(years=args.years, departments=args.departments))
