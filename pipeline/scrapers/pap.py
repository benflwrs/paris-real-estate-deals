"""PAP.fr scraper (particuliers only, no agencies).

PAP now sits behind Cloudflare's bot-challenge, so plain httpx requests get
a 403 "Just a moment..." interstitial. We use Playwright with a real
Chromium context to pass the challenge and read the rendered page's
embedded JSON (PAP ships a `window.__NUXT__` / JSON-LD block with
structured listing data -- prefer that over fragile CSS selectors).

Kept deliberately low-volume/low-concurrency: PAP is a smaller site and a
lower priority than Bien'ici; treat it gently.
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

SEARCH_URL_TMPL = "https://www.pap.fr/annonce/vente-appartement-maison-{city_slug}-g{geo_id}"

# Common Ile-de-France geo IDs used by PAP's own search URLs (g<id> path segment).
# Extend this table as more zones are needed.
KNOWN_GEO_IDS = {
    "paris": "439",
}


def _extract_json_ld_listings(html: str) -> list[dict]:
    """Extract structured listing data from JSON-LD <script> blocks in the page."""
    blocks = re.findall(
        r'<script type="application/ld\+json"[^>]*>(.*?)</script>', html, re.DOTALL
    )
    listings = []
    for block in blocks:
        try:
            data = json.loads(block)
        except json.JSONDecodeError:
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if isinstance(item, dict) and item.get("@type") in ("Product", "Offer", "RealEstateListing"):
                listings.append(item)
    return listings


def _to_listing_from_card(card: dict, transaction_type: str) -> Optional[Listing]:
    """Map a PAP search-result card (from the page's client-side JSON state)
    into our normalized Listing. `card` shape depends on PAP's current
    Nuxt/Vue state -- keep this function isolated so it's the one place to
    fix when the site changes."""
    try:
        source_id = str(card.get("id") or card.get("item_id"))
        if not source_id or source_id == "None":
            return None
        return Listing(
            source="pap",
            source_id=source_id,
            url=card.get("url") or f"https://www.pap.fr/annonce/{source_id}",
            transaction_type=transaction_type,
            price=card.get("price") or card.get("prix"),
            surface_m2=card.get("surface") or card.get("surface_field"),
            rooms=card.get("nb_pieces") or card.get("rooms"),
            bedrooms=card.get("nb_chambres") or card.get("bedrooms"),
            property_type=card.get("type_bien") or card.get("property_type"),
            floor=card.get("etage") or card.get("floor"),
            has_elevator=card.get("ascenseur"),
            construction_year=card.get("annee_construction"),
            is_furnished=card.get("meuble"),
            has_balcony=card.get("balcon"),
            has_parking=card.get("parking") or card.get("garage"),
            postal_code=card.get("code_postal") or card.get("postal_code"),
            city=card.get("ville") or card.get("city"),
            longitude=card.get("lng") or card.get("longitude"),
            latitude=card.get("lat") or card.get("latitude"),
            raw_json=card,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("failed to map PAP card: %s", exc)
        return None


def fetch_search_page_html(url: str, page) -> str:
    """Load a PAP search URL in a Playwright page and return rendered HTML,
    waiting out the Cloudflare challenge if present."""
    page.goto(url, wait_until="domcontentloaded", timeout=30000)
    # Cloudflare interstitial resolves client-side; give it a moment.
    for _ in range(10):
        title = page.title()
        if "Just a moment" not in title:
            break
        time.sleep(1)
    page.wait_for_timeout(1500)
    return page.content()


def iter_listings_from_browser(
    city_slug: str = "paris",
    geo_id: Optional[str] = None,
    transaction_type: str = "sale",
    max_pages: int = 5,
    browser_context=None,
) -> Iterator[Listing]:
    """Yield Listings for a PAP search, driving a Playwright browser context
    supplied by the caller (keeps this module testable / decoupled from
    playwright's own lifecycle management)."""
    geo_id = geo_id or KNOWN_GEO_IDS.get(city_slug)
    if not geo_id:
        raise ValueError(f"no known geo_id for city_slug={city_slug!r}; add it to KNOWN_GEO_IDS")

    page = browser_context.new_page()
    try:
        for page_num in range(1, max_pages + 1):
            url = SEARCH_URL_TMPL.format(city_slug=city_slug, geo_id=geo_id)
            if page_num > 1:
                url += f"?offre={page_num}"
            html = fetch_search_page_html(url, page)
            cards = _extract_json_ld_listings(html)
            if not cards:
                break
            for card in cards:
                listing = _to_listing_from_card(card, transaction_type)
                if listing:
                    yield listing
            time.sleep(random.uniform(3, 6))
    finally:
        page.close()


def run(city_slug: str = "paris", transaction_type: str = "sale", max_pages: int = 5) -> dict:
    """Full scrape run using a fresh Playwright browser. Requires the
    `playwright` package and a Chromium binary (`playwright install chromium`)."""
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
                    city_slug, transaction_type=transaction_type, max_pages=max_pages,
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

        mark_stale_inactive(conn, "pap", seen_ids)
        return {"city_slug": city_slug, "written": written, "seen_ids": len(seen_ids)}
    except Exception as exc:  # noqa: BLE001
        log_scrape_error(conn, "pap", str(exc))
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--city-slug", default="paris")
    parser.add_argument("--transaction-type", default="sale", choices=["sale", "rent"])
    parser.add_argument("--max-pages", type=int, default=5)
    args = parser.parse_args()
    print(run(args.city_slug, args.transaction_type, args.max_pages))
