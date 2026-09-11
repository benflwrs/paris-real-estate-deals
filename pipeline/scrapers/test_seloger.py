from pipeline.scrapers.seloger import _extract_next_data, _find_listing_cards, _to_listing_from_card

SAMPLE_HTML = """
<html><body>
<script id="__NEXT_DATA__" type="application/json">
{"props":{"pageProps":{"searchResults":{"listings":[
  {"id":"111","price":500000,"surface":70,"rooms":3,"permalink":"https://www.seloger.com/annonces/111",
   "location":{"postalCode":"75015","city":"Paris","coordinates":{"lat":48.84,"lon":2.29}},
   "elevator":true,"energyClass":"D"},
  {"id":"222","pricing":{"price":610000},"surface":80,"rooms":4,
   "location":{"postalCode":"75012","city":"Paris"}}
]}}}}
</script>
</body></html>
"""


def test_extract_next_data_parses_json():
    data = _extract_next_data(SAMPLE_HTML)
    assert data is not None
    assert "props" in data


def test_extract_next_data_returns_none_when_missing():
    assert _extract_next_data("<html><body>no next data</body></html>") is None


def test_find_listing_cards_walks_nested_tree():
    data = _extract_next_data(SAMPLE_HTML)
    cards = _find_listing_cards(data)
    ids = {c["id"] for c in cards}
    assert ids == {"111", "222"}


def test_to_listing_from_card_maps_direct_price():
    data = _extract_next_data(SAMPLE_HTML)
    cards = {c["id"]: c for c in _find_listing_cards(data)}
    listing = _to_listing_from_card(cards["111"], "sale")
    assert listing.price == 500000
    assert listing.surface_m2 == 70
    assert listing.postal_code == "75015"
    assert listing.latitude == 48.84
    assert listing.validate() == []


def test_to_listing_from_card_maps_nested_pricing():
    data = _extract_next_data(SAMPLE_HTML)
    cards = {c["id"]: c for c in _find_listing_cards(data)}
    listing = _to_listing_from_card(cards["222"], "sale")
    assert listing.price == 610000


def test_to_listing_from_card_returns_none_without_id():
    assert _to_listing_from_card({"price": 100}, "sale") is None
