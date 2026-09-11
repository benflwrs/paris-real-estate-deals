"""LeBonCoin.fr scraper.

LeBonCoin is protected by DataDome; both its HTML search pages and its
internal `api.leboncoin.fr/finder/search` API return DataDome captcha
challenges to bare HTTP clients. Requires a real Playwright browser
context. Once loaded, search results are embedded in a
`<script id="__NEXT_DATA__">` JSON blob (Next.js SSR), same pattern as
SeLoger -- parse that directly.

LeBonCoin masks seller phone/email (GDPR) but does expose GPS coordinates
per listing, which is what we need for scoring.

Accepted risk for personal project use (per user instruction). Keep
concurrency low and expect this to need maintenance over time.
"""
from __future__ import annotations

import json
import logging
import re
import time
import random
from typing import Iterator, Optional

from pipeline.scrapers.base import Listing, get_db_connection, upsert_listings, mark_stale_inactive, log_scrape_error

logger = logging.getLogger(__name__)

SEARCH_URL_TMPL = (
    "https://www.leboncoin.fr/recherche?category=9&locations={location}"
    "&real_estate_type={real_estate_type}&page={page}"
)

# category=9 -> ventes immobilieres (real estate sales) on leboncoin.
# real_estate_type: 1=maison, 2=appartement
KNOWN_LOCATIONS = {
    "paris": "Paris_75000",
}


def _extract_next_data(html: str) -> Optional[dict]:
    match = re.search(
        r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL
    )
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError:
        logger.warning("failed to parse __NEXT_DATA__ JSON")
        return None


def _find_ads(next_data: dict) -> list[dict]:
    """LeBonCoin's Next.js state nests search results under a shifting path;
    walk the tree for dicts that look like an ad (list_id + price attrs)."""
    ads: list[dict] = []

    def looks_like_ad(d: dict) -> bool:
        return "list_id" in d and ("price" in d or "price_cents" in d)

    def walk(node):
        if isinstance(node, dict):
            if looks_like_ad(node):
                ads.append(node)
                return
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(next_data.get("props", {}))
    return ads


def _get_attr(ad: dict, key: str):
    """LeBonCoin ads store most structured fields inside an `attributes`
    list of {key, value} dicts rather than top-level keys."""
    for attr in ad.get("attributes", []) or []:
        if attr.get("key") == key:
            return attr.get("value")
    return None


def _to_listing(ad: dict, transaction_type: str) -> Optional[Listing]:
    try:
        source_id = str(ad.get("list_id"))
        if not source_id or source_id == "None":
            return None

        price = ad.get("price")
        if isinstance(price, list):
            price = price[0] if price else None
        if price is None and ad.get("price_cents"):
            price = ad["price_cents"] / 100

        location = ad.get("location") or {}

        surface = _get_attr(ad, "square")
        rooms = _get_attr(ad, "rooms")
        property_type = _get_attr(ad, "real_estate_type")
        floor = _get_attr(ad, "floor_number")
        elevator = _get_attr(ad, "elevator")
        furnished = _get_attr(ad, "furnished")
        dpe = _get_attr(ad, "energy_rate") or _get_attr(ad, "dpe_class")

        return Listing(
            source="leboncoin",
            source_id=source_id,
            url=ad.get("url") or f"https://www.leboncoin.fr/ad/{source_id}",
            transaction_type=transaction_type,
            price=float(price) if price is not None else None,
            surface_m2=float(surface) if surface else None,
            rooms=int(rooms) if rooms else None,
            property_type=property_type,
            floor=int(floor) if floor else None,
            has_elevator=(elevator == "1" or elevator is True) if elevator is not None else None,
            is_furnished=(furnished == "1" or furnished is True) if furnished is not None else None,
            dpe_class=dpe,
            postal_code=location.get("zipcode"),
            city=location.get("city"),
            longitude=location.get("lng"),
            latitude=location.get("lat"),
            raw_json=ad,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("failed to map LeBonCoin ad: %s", exc)
        return None


def fetch_search_page_html(url: str, page) -> str:
    page.goto(url, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(2000)
    return page.content()


def iter_listings_from_browser(
    city_key: str = "paris",
    location: Optional[str] = None,
    real_estate_type: str = "2",
    transaction_type: str = "sale",
    max_pages: int = 5,
    browser_context=None,
) -> Iterator[Listing]:
    location = location or KNOWN_LOCATIONS.get(city_key)
    if not location:
        raise ValueError(f"no known location for city_key={city_key!r}; add it to KNOWN_LOCATIONS")

    page = browser_context.new_page()
    try:
        for page_num in range(1, max_pages + 1):
            url = SEARCH_URL_TMPL.format(location=location, real_estate_type=real_estate_type, page=page_num)
            html = fetch_search_page_html(url, page)
            next_data = _extract_next_data(html)
            if not next_data:
                logger.warning("no __NEXT_DATA__ found on page %s -- site markup may have changed", page_num)
                break
            ads = _find_ads(next_data)
            if not ads:
                break
            for ad in ads:
                listing = _to_listing(ad, transaction_type)
                if listing:
                    yield listing
            time.sleep(random.uniform(3, 7))
    finally:
        page.close()


def run(city_key: str = "paris", transaction_type: str = "sale", max_pages: int = 5) -> dict:
    from playwright.sync_api import sync_playwright

    conn = get_db_connection()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
                )
            )
            seen_ids: set[str] = set()
            batch: list[Listing] = []
            written = 0
            try:
                for listing in iter_listings_from_browser(
                    city_key, transaction_type=transaction_type, max_pages=max_pages,
                    browser_context=context,
                ):
                    seen_ids.add(listing.source_id)
                    batch.append(listing)
                    if len(batch) >= 50:
                        written += upsert_listings(conn, batch)
                        batch = []
                if batch:
                    written += upsert_listings(conn, batch)
            finally:
                context.close()
                browser.close()

        mark_stale_inactive(conn, "leboncoin", seen_ids)
        return {"city_key": city_key, "written": written, "seen_ids": len(seen_ids)}
    except Exception as exc:  # noqa: BLE001
        log_scrape_error(conn, "leboncoin", str(exc))
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--city-key", default="paris")
    parser.add_argument("--transaction-type", default="sale", choices=["sale", "rent"])
    parser.add_argument("--max-pages", type=int, default=5)
    args = parser.parse_args()
    print(run(args.city_key, args.transaction_type, args.max_pages))
