from pipeline.scrapers.pap import _extract_json_ld_listings, _to_listing_from_card

SAMPLE_HTML = """
<html><head>
<script type="application/ld+json">
{"@type":"RealEstateListing","id":"12345","url":"https://www.pap.fr/annonce/12345",
 "price":450000,"surface":65,"nb_pieces":3,"nb_chambres":2,"type_bien":"appartement",
 "etage":3,"ascenseur":true,"ville":"Paris","code_postal":"75011","lat":48.86,"lng":2.38}
</script>
<script type="application/ld+json">
{"@type":"Thing","name":"irrelevant"}
</script>
</head><body></body></html>
"""


def test_extract_json_ld_listings_finds_real_estate_blocks():
    listings = _extract_json_ld_listings(SAMPLE_HTML)
    assert len(listings) == 1
    assert listings[0]["id"] == "12345"


def test_extract_json_ld_listings_ignores_non_listing_types():
    html = '<script type="application/ld+json">{"@type":"Organization"}</script>'
    assert _extract_json_ld_listings(html) == []


def test_extract_json_ld_listings_handles_malformed_json():
    html = '<script type="application/ld+json">{not valid json</script>'
    assert _extract_json_ld_listings(html) == []


def test_to_listing_from_card_maps_fields():
    card = {
        "id": "12345", "url": "https://www.pap.fr/annonce/12345",
        "price": 450000, "surface": 65, "nb_pieces": 3, "nb_chambres": 2,
        "type_bien": "appartement", "etage": 3, "ascenseur": True,
        "ville": "Paris", "code_postal": "75011", "lat": 48.86, "lng": 2.38,
    }
    listing = _to_listing_from_card(card, "sale")
    assert listing is not None
    assert listing.source == "pap"
    assert listing.source_id == "12345"
    assert listing.price == 450000
    assert listing.surface_m2 == 65
    assert listing.rooms == 3
    assert listing.has_elevator is True
    assert listing.postal_code == "75011"
    assert listing.validate() == []


def test_to_listing_from_card_returns_none_without_id():
    card = {"price": 100000}
    assert _to_listing_from_card(card, "sale") is None
