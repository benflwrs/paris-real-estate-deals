"""Bien'ici scraper.

Bien'ici exposes an (unofficial but stable, widely used) JSON search API:
  - https://res.bienici.com/suggest.json?q=<place>   -> resolve a place name to zoneIds
  - https://www.bienici.com/realEstateAds.json?filters=<json> -> paginated listings

No auth, no proxy needed. Be polite: cap concurrency at 1, add a delay
between page requests. Bien'ici caps result windows at ~2400-2500 rows per
query ("Too many ads requested" past that) -- narrow by price/property type
if a single zone search needs more coverage than that.
"""
from __future__ import annotations

import json
import time
import random
import logging
from typing import Iterator, Optional

import httpx

from pipeline.scrapers.base import Listing, get_db_connection, upsert_listings, mark_stale_inactive, log_scrape_error

logger = logging.getLogger(__name__)

BASE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json",
}

SUGGEST_URL = "https://res.bienici.com/suggest.json"
SEARCH_URL = "https://www.bienici.com/realEstateAds.json"
PAGE_SIZE = 24
MAX_RESULT_WINDOW = 2400  # bien'ici hard cap per query


def resolve_zone_ids(place: str, client: Optional[httpx.Client] = None) -> list[str]:
    """Resolve a free-text place name (city, arrondissement, postal code)
    to Bien'ici zoneIds via their suggest endpoint."""
    own_client = client is None
    client = client or httpx.Client(headers=BASE_HEADERS, timeout=20)
    try:
        resp = client.get(SUGGEST_URL, params={"q": place})
        resp.raise_for_status()
        results = resp.json()
        if not results:
            return []
        return results[0].get("zoneIds", [])
    finally:
        if own_client:
            client.close()


def _to_listing(ad: dict, transaction_type: str) -> Listing:
    position = (ad.get("blurInfo") or {}).get("position") or {}
    district = ad.get("district") or {}
    insee_code = district.get("insee_code")

    # New-development ("programme neuf") ads from promoters (e.g. Marignan)
    # expose a price *range* (list) across multiple lots rather than a
    # single price -- take the lower bound as a conservative estimate.
    price = ad.get("price")
    if isinstance(price, list):
        price = min(price) if price else None

    return Listing(
        source="bienici",
        source_id=str(ad.get("id")),
        url=f"https://www.bienici.com/annonce/{ad.get('id')}",
        transaction_type=transaction_type,
        price=price,
        surface_m2=ad.get("surfaceArea"),
        rooms=ad.get("roomsQuantity"),
        bedrooms=ad.get("bedroomsQuantity"),
        property_type=ad.get("propertyType"),
        floor=ad.get("floor"),
        floor_count=ad.get("floorQuantity"),
        has_elevator=ad.get("hasElevator"),
        in_residence=ad.get("isInCondominium"),
        construction_year=ad.get("yearOfConstruction"),
        is_furnished=ad.get("furnished"),
        has_balcony=ad.get("hasBalcony"),
        has_parking=(ad.get("parkingPlacesQuantity") or 0) > 0,
        dpe_class=ad.get("energyClassification"),
        address_raw=district.get("libelle") or ad.get("city"),
        postal_code=ad.get("postalCode"),
        city=ad.get("city"),
        insee_code=insee_code,
        longitude=position.get("lon"),
        latitude=position.get("lat"),
        raw_json=ad,
    )


def iter_listings(
    zone_ids: list[str],
    transaction_type: str = "buy",
    property_types: Optional[list[str]] = None,
    max_results: Optional[int] = None,
    client: Optional[httpx.Client] = None,
    delay_range: tuple[float, float] = (2.0, 4.0),
) -> Iterator[Listing]:
    """Yield Listing objects for a zone search, paginating politely."""
    property_types = property_types or ["flat", "house"]
    own_client = client is None
    client = client or httpx.Client(headers=BASE_HEADERS, timeout=20)
    yielded = 0
    page = 1
    try:
        while True:
            offset = (page - 1) * PAGE_SIZE
            if offset >= MAX_RESULT_WINDOW:
                logger.warning("hit bien'ici result window cap (%s); stop paginating", MAX_RESULT_WINDOW)
                break
            filters = {
                "size": PAGE_SIZE,
                "from": offset,
                "showAllModels": False,
                "filterType": transaction_type,
                "propertyType": property_types,
                "page": page,
                "sortBy": "relevance",
                "sortOrder": "desc",
                "onTheMarket": [True],
                "zoneIdsByTypes": {"zoneIds": zone_ids},
            }
            resp = client.get(SEARCH_URL, params={"filters": json.dumps(filters)})
            resp.raise_for_status()
            data = resp.json()
            ads = data.get("realEstateAds", [])
            if not ads:
                break
            for ad in ads:
                mapped_type = "sale" if transaction_type == "buy" else "rent"
                yield _to_listing(ad, mapped_type)
                yielded += 1
                if max_results and yielded >= max_results:
                    return
            page += 1
            time.sleep(random.uniform(*delay_range))
    finally:
        if own_client:
            client.close()


def run(place: str = "Paris", transaction_type: str = "buy", max_results: Optional[int] = None) -> dict:
    """Full scrape run for one place: resolve zone, page through listings,
    upsert to DB, mark stale listings inactive."""
    conn = get_db_connection()
    try:
        zone_ids = resolve_zone_ids(place)
        if not zone_ids:
            log_scrape_error(conn, "bienici", f"could not resolve zoneIds for place={place!r}")
            return {"place": place, "written": 0, "seen_ids": 0}

        seen_ids: set[str] = set()
        batch: list[Listing] = []
        written = 0
        for listing in iter_listings(zone_ids, transaction_type=transaction_type, max_results=max_results):
            seen_ids.add(listing.source_id)
            batch.append(listing)
            if len(batch) >= 100:
                written += upsert_listings(conn, batch)
                batch = []
        if batch:
            written += upsert_listings(conn, batch)

        mark_stale_inactive(conn, "bienici", seen_ids)
        return {"place": place, "written": written, "seen_ids": len(seen_ids)}
    except Exception as exc:  # noqa: BLE001 - keep scraper alive across runs
        log_scrape_error(conn, "bienici", str(exc))
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--place", default="Paris")
    parser.add_argument("--transaction-type", default="buy", choices=["buy", "rent"])
    parser.add_argument("--max-results", type=int, default=None)
    args = parser.parse_args()

    result = run(args.place, args.transaction_type, args.max_results)
    print(result)
