"""Shared data model and DB helpers for all scrapers."""
from __future__ import annotations

import os
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional, Any

from dotenv import load_dotenv

load_dotenv()


@dataclass
class Listing:
    """Normalized representation of a single real-estate listing.

    Every scraper must map its site-specific payload into this shape
    before it touches the database.
    """

    source: str  # 'bienici' | 'pap' | 'seloger' | 'leboncoin'
    source_id: str
    url: str
    transaction_type: str  # 'sale' | 'rent'
    price: Optional[float] = None
    surface_m2: Optional[float] = None
    rooms: Optional[int] = None
    bedrooms: Optional[int] = None
    property_type: Optional[str] = None
    floor: Optional[int] = None
    floor_count: Optional[int] = None
    has_elevator: Optional[bool] = None
    in_residence: Optional[bool] = None
    construction_year: Optional[int] = None
    is_furnished: Optional[bool] = None
    has_balcony: Optional[bool] = None
    has_parking: Optional[bool] = None
    dpe_class: Optional[str] = None
    address_raw: Optional[str] = None
    postal_code: Optional[str] = None
    city: Optional[str] = None
    insee_code: Optional[str] = None
    longitude: Optional[float] = None
    latitude: Optional[float] = None
    raw_json: dict[str, Any] = field(default_factory=dict)

    @property
    def price_per_m2(self) -> Optional[float]:
        if (
            isinstance(self.price, (int, float))
            and isinstance(self.surface_m2, (int, float))
            and self.surface_m2 > 0
        ):
            return round(self.price / self.surface_m2, 2)
        return None

    def validate(self) -> list[str]:
        """Return a list of validation problems (empty = OK)."""
        problems = []
        if not self.source_id:
            problems.append("missing source_id")
        if not self.url:
            problems.append("missing url")
        if self.transaction_type not in ("sale", "rent"):
            problems.append(f"invalid transaction_type: {self.transaction_type!r}")
        if self.price is not None and not isinstance(self.price, (int, float)):
            problems.append(f"price is not numeric: {type(self.price)}")
        elif self.price is not None and self.price < 0:
            problems.append("negative price")
        if self.surface_m2 is not None and not isinstance(self.surface_m2, (int, float)):
            problems.append(f"surface_m2 is not numeric: {type(self.surface_m2)}")
        elif self.surface_m2 is not None and self.surface_m2 <= 0:
            problems.append("non-positive surface_m2")
        return problems


def get_db_connection():
    """Create a psycopg2 connection from env vars (lazy import to keep
    unit tests importable without psycopg2/a live DB)."""
    import psycopg2

    return psycopg2.connect(
        host=os.getenv("POSTGRES_HOST", "127.0.0.1"),
        port=int(os.getenv("POSTGRES_PORT", "5432")),
        user=os.getenv("POSTGRES_USER", "paris"),
        password=os.getenv("POSTGRES_PASSWORD", "paris_dev_pw"),
        dbname=os.getenv("POSTGRES_DB", "paris_deals"),
    )


UPSERT_SQL = """
INSERT INTO listings (
    source, source_id, url, transaction_type, price, price_per_m2, surface_m2,
    rooms, bedrooms, property_type, floor, floor_count, has_elevator,
    in_residence, construction_year, is_furnished, has_balcony, has_parking,
    dpe_class, address_raw, postal_code, city, insee_code, geom, raw_json,
    first_seen_at, last_seen_at, is_active
) VALUES (
    %(source)s, %(source_id)s, %(url)s, %(transaction_type)s, %(price)s,
    %(price_per_m2)s, %(surface_m2)s, %(rooms)s, %(bedrooms)s, %(property_type)s,
    %(floor)s, %(floor_count)s, %(has_elevator)s, %(in_residence)s,
    %(construction_year)s, %(is_furnished)s, %(has_balcony)s, %(has_parking)s,
    %(dpe_class)s, %(address_raw)s, %(postal_code)s, %(city)s, %(insee_code)s,
    ST_SetSRID(ST_MakePoint(%(longitude)s, %(latitude)s), 4326)::geography,
    %(raw_json)s, now(), now(), true
)
ON CONFLICT (source, source_id) DO UPDATE SET
    price = EXCLUDED.price,
    price_per_m2 = EXCLUDED.price_per_m2,
    last_seen_at = now(),
    is_active = true,
    raw_json = EXCLUDED.raw_json;
"""


def upsert_listings(conn, listings: list[Listing]) -> int:
    """Upsert a batch of Listing objects. Returns count written.
    Skips (and logs) invalid listings rather than failing the whole batch.
    """
    import json
    from psycopg2.extras import execute_batch

    rows = []
    for listing in listings:
        problems = listing.validate()
        if problems:
            log_scrape_error(conn, listing.source, f"invalid listing {listing.source_id}: {problems}")
            continue
        d = asdict(listing)
        d["price_per_m2"] = listing.price_per_m2
        d["raw_json"] = json.dumps(d.pop("raw_json"))
        d.pop("longitude", None)
        d.pop("latitude", None)
        d["longitude"] = listing.longitude
        d["latitude"] = listing.latitude
        rows.append(d)

    if not rows:
        return 0

    with conn.cursor() as cur:
        execute_batch(cur, UPSERT_SQL, rows, page_size=200)
    conn.commit()
    return len(rows)


def mark_stale_inactive(conn, source: str, seen_source_ids: set[str]) -> int:
    """Mark listings from `source` not present in seen_source_ids as inactive.
    Call after a full scrape pass for that source."""
    with conn.cursor() as cur:
        if seen_source_ids:
            cur.execute(
                """
                UPDATE listings SET is_active = false
                WHERE source = %s AND is_active = true AND source_id != ALL(%s)
                """,
                (source, list(seen_source_ids)),
            )
        else:
            cur.execute(
                "UPDATE listings SET is_active = false WHERE source = %s AND is_active = true",
                (source,),
            )
        count = cur.rowcount
    conn.commit()
    return count


def log_scrape_error(conn, source: str, error_text: str, context: Optional[dict] = None) -> None:
    import json

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO scrape_errors (source, error_text, context) VALUES (%s, %s, %s)",
            (source, error_text, json.dumps(context or {})),
        )
    conn.commit()
