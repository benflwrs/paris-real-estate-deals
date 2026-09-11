"""Orchestrates a full scrape pass across all sources, then triggers
downstream stale-marking. Designed to be invoked by cron on the VPS.

Order: Bien'ici and PAP first (lighter, lower risk), then SeLoger and
LeBonCoin (heavier, higher anti-bot risk) -- run those less frequently in
the actual cron schedule (see infra/cron notes in the plan doc).
"""
from __future__ import annotations

import logging
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)


def run_bienici(place: str = "Paris") -> dict:
    from pipeline.scrapers import bienici

    logger.info("running bienici scraper for %s", place)
    return bienici.run(place=place, transaction_type="buy")


def run_pap(city_slug: str = "paris") -> dict:
    from pipeline.scrapers import pap

    logger.info("running pap scraper for %s", city_slug)
    return pap.run(city_slug=city_slug, transaction_type="sale")


def run_seloger(city_key: str = "paris") -> dict:
    from pipeline.scrapers import seloger

    logger.info("running seloger scraper for %s", city_key)
    return seloger.run(city_key=city_key, transaction_type="sale")


def run_leboncoin(city_key: str = "paris") -> dict:
    from pipeline.scrapers import leboncoin

    logger.info("running leboncoin scraper for %s", city_key)
    return leboncoin.run(city_key=city_key, transaction_type="sale")


SCRAPERS = {
    "bienici": run_bienici,
    "pap": run_pap,
    "seloger": run_seloger,
    "leboncoin": run_leboncoin,
}


def run_all(sources: list[str] | None = None) -> dict:
    sources = sources or list(SCRAPERS.keys())
    results = {}
    for source in sources:
        fn = SCRAPERS.get(source)
        if not fn:
            logger.warning("unknown source: %s", source)
            continue
        try:
            results[source] = fn()
        except Exception as exc:  # noqa: BLE001 - one bad scraper shouldn't kill the run
            logger.error("scraper %s failed: %s", source, exc)
            results[source] = {"error": str(exc)}
    return results


if __name__ == "__main__":
    import argparse
    import json

    parser = argparse.ArgumentParser()
    parser.add_argument("--sources", nargs="+", default=None, choices=list(SCRAPERS.keys()))
    args = parser.parse_args()

    outcome = run_all(args.sources)
    print(json.dumps(outcome, indent=2, default=str))

    # Non-zero exit if every scraper failed, so cron/alerting can catch it.
    if all(isinstance(v, dict) and "error" in v for v in outcome.values()):
        sys.exit(1)
