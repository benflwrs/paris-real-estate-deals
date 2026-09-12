import json
import os
from unittest.mock import MagicMock

import pytest

from pipeline.scrapers.base import Listing
from pipeline.scrapers.bienici import _to_listing, iter_listings, resolve_zone_ids

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures")


def load_fixture(name: str) -> dict:
    with open(os.path.join(FIXTURE_DIR, name), encoding="utf-8") as f:
        return json.load(f)


def test_to_listing_maps_core_fields():
    ad = load_fixture("bienici_sample_ad.json")
    listing = _to_listing(ad, "sale")

    assert listing.source == "bienici"
    assert listing.source_id == str(ad["id"])
    assert listing.transaction_type == "sale"
    assert listing.price == ad["price"]
    assert listing.surface_m2 == ad["surfaceArea"]
    assert listing.rooms == ad["roomsQuantity"]
    assert listing.bedrooms == ad["bedroomsQuantity"]
    assert listing.floor == ad["floor"]
    assert listing.floor_count == ad["floorQuantity"]
    assert listing.has_elevator == ad["hasElevator"]
    assert listing.construction_year == ad["yearOfConstruction"]
    assert listing.dpe_class == ad["energyClassification"]
    assert listing.postal_code == ad["postalCode"]
    assert listing.insee_code == ad["district"]["insee_code"]
    assert listing.latitude == pytest.approx(ad["blurInfo"]["position"]["lat"])
    assert listing.longitude == pytest.approx(ad["blurInfo"]["position"]["lon"])


def test_to_listing_produces_valid_listing():
    ad = load_fixture("bienici_sample_ad.json")
    listing = _to_listing(ad, "sale")
    assert listing.validate() == []


def test_to_listing_takes_min_of_price_range_for_new_developments():
    ad = load_fixture("bienici_sample_ad.json")
    ad = dict(ad)
    ad["price"] = [654200, 664000]
    listing = _to_listing(ad, "sale")
    assert listing.price == 654200
    assert listing.validate() == []


def test_price_per_m2_computed():
    ad = load_fixture("bienici_sample_ad.json")
    listing = _to_listing(ad, "sale")
    expected = round(ad["price"] / ad["surfaceArea"], 2)
    assert listing.price_per_m2 == expected


def test_iter_listings_paginates_and_stops_on_empty_page():
    ad = load_fixture("bienici_sample_ad.json")
    page1_resp = MagicMock()
    page1_resp.json.return_value = {"total": 2, "realEstateAds": [ad]}
    page1_resp.raise_for_status.return_value = None
    page2_resp = MagicMock()
    page2_resp.json.return_value = {"total": 2, "realEstateAds": []}
    page2_resp.raise_for_status.return_value = None

    client = MagicMock()
    client.get.side_effect = [page1_resp, page2_resp]

    results = list(
        iter_listings(["-7444"], transaction_type="buy", client=client, delay_range=(0, 0))
    )
    assert len(results) == 1
    assert results[0].source_id == str(ad["id"])
    assert results[0].transaction_type == "sale"


def test_iter_listings_respects_max_results():
    ad = load_fixture("bienici_sample_ad.json")
    resp = MagicMock()
    resp.json.return_value = {"total": 10, "realEstateAds": [ad, ad, ad]}
    resp.raise_for_status.return_value = None

    client = MagicMock()
    client.get.return_value = resp

    results = list(
        iter_listings(["-7444"], client=client, max_results=2, delay_range=(0, 0))
    )
    assert len(results) == 2


def test_resolve_zone_ids_parses_suggest_response():
    resp = MagicMock()
    resp.json.return_value = [{"zoneIds": ["-7444"], "name": "Paris"}]
    resp.raise_for_status.return_value = None
    client = MagicMock()
    client.get.return_value = resp

    zone_ids = resolve_zone_ids("Paris", client=client)
    assert zone_ids == ["-7444"]


def test_resolve_zone_ids_empty_when_no_match():
    resp = MagicMock()
    resp.json.return_value = []
    resp.raise_for_status.return_value = None
    client = MagicMock()
    client.get.return_value = resp

    assert resolve_zone_ids("Nonexistentplace123", client=client) == []
