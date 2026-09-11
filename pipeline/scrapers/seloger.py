"""SeLoger.com scraper.

SeLoger is protected by DataDome (bot-challenge) -- plain HTTP requests get
a 403 JS-challenge page. Requires a real browser (Playwright) with a
persistent context. Once rendered, SeLoger's search results are embedded in
a `<script id="__NEXT_DATA__">` JSON blob (Next.js SSR) -- parse that
directly instead of fragile CSS selectors, which is far less likely to
break on minor markup changes.

NOTE: SeLoger has actively pursued legal action against scrapers (see
Jinka court case, Dec 2025). This is accepted risk for a personal project
per user instruction -- keep concurrency low (1-2 browsers), randomized
delays, and expect this scraper to need maintenance whenever SeLoger
changes its anti-bot posture.
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

SEARCH_URL_TMPL = "https://www.seloger.com/list.htm?projects=2&types=1,2&places=[{{ci:{insee_code}}}]&LISTING-LISTpg={page}"

# Common Ile-de-France INSEE codes for SeLoger's `ci:` place filter.
KNOWN_INSEE_CODES = {
    "paris": "750056",
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


def _find_listing_cards(next_data: dict) -> list[dict]:
    """SeLoger nests search results somewhere under
    props.pageProps.<...>.listings (exact path shifts between releases) --
    walk the tree looking for a list of dicts that look like listing cards
    (have both 'id' and a price-ish field) rather than hardcoding one path.
    """
    cards: list[dict] = []

    def looks_like_card(d: dict) -> bool:
        has_id = "id" in d or "listingId" in d
        has_price = any(k in d for k in ("price", "pricing", "prixHint"))
        return has_id and has_price

    def walk(node):
        if isinstance(node, dict):
            if looks_like_card(node):
                cards.append(node)
                return
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(next_data.get("props", {}))
    return cards


def _to_listing_from_card(card: dict, transaction_type: str) -> Optional[Listing]:
    try:
        source_id = str(card.get("id") or card.get("listingId"))
        if not source_id or source_id == "None":
            return None

        price = card.get("price")
        if price is None and isinstance(card.get("pricing"), dict):
            price = card["pricing"].get("price") or card["pricing"].get("value")

        surface = card.get("surface") or card.get("livingArea")
        location = card.get("location") or {}
        coords = location.get("coordinates") or {}

        return Listing(
            source="seloger",
            source_id=source_id,
            url=card.get("permalink") or card.get("url") or f"https://www.seloger.com/annonces/{source_id}",
            transaction_type=transaction_type,
            price=price,
            surface_m2=surface,
            rooms=card.get("rooms") or card.get("roomsCount"),
            bedrooms=card.get("bedrooms") or card.get("bedroomsCount"),
            property_type=card.get("propertyType") or card.get("estateType"),
            floor=card.get("floor"),
            has_elevator=card.get("elevator"),
            postal_code=location.get("postalCode") or card.get("postalCode"),
            city=location.get("city") or card.get("city"),
            longitude=coords.get("lon") or coords.get("lng"),
            latitude=coords.get("lat"),
            dpe_class=card.get("energyClass") or card.get("dpeClass"),
            raw_json=card,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("failed to map SeLoger card: %s", exc)
        return None


def fetch_search_page_html(url: str, page) -> str:
    page.goto(url, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(2000)
    return page.content()


def iter_listings_from_browser(
    city_key: str = "paris",
    insee_code: Optional[str] = None,
    transaction_type: str = "sale",
    max_pages: int = 5,
    browser_context=None,
) -> Iterator[Listing]:
    insee_code = insee_code or KNOWN_INSEE_CODES.get(city_key)
    if not insee_code:
        raise ValueError(f"no known insee_code for city_key={city_key!r}; add it to KNOWN_INSEE_CODES")

    page = browser_context.new_page()
    try:
        for page_num in range(1, max_pages + 1):
            url = SEARCH_URL_TMPL.format(insee_code=insee_code, page=page_num)
            html = fetch_search_page_html(url, page)
            next_data = _extract_next_data(html)
            if not next_data:
                logger.warning("no __NEXT_DATA__ found on page %s -- site markup may have changed", page_num)
                break
            cards = _find_listing_cards(next_data)
            if not cards:
                break
            for card in cards:
                listing = _to_listing_from_card(card, transaction_type)
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

        mark_stale_inactive(conn, "seloger", seen_ids)
        return {"city_key": city_key, "written": written, "seen_ids": len(seen_ids)}
    except Exception as exc:  # noqa: BLE001
        log_scrape_error(conn, "seloger", str(exc))
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
