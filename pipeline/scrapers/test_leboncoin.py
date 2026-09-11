from pipeline.scrapers.leboncoin import _extract_next_data, _find_ads, _get_attr, _to_listing

SAMPLE_HTML = """
<html><body>
<script id="__NEXT_DATA__" type="application/json">
{"props":{"pageProps":{"searchData":{"ads":[
  {"list_id":111,"price":[500000],"url":"https://www.leboncoin.fr/ad/111",
   "location":{"zipcode":"75015","city":"Paris","lat":48.84,"lng":2.29},
   "attributes":[{"key":"square","value":"70"},{"key":"rooms","value":"3"},
                 {"key":"real_estate_type","value":"2"},{"key":"elevator","value":"1"}]},
  {"list_id":222,"price_cents":61000000,
   "location":{"zipcode":"75012","city":"Paris"},
   "attributes":[{"key":"square","value":"80"}]}
]}}}}
</script>
</body></html>
"""


def test_extract_next_data_parses_json():
    data = _extract_next_data(SAMPLE_HTML)
    assert data is not None


def test_extract_next_data_none_when_missing():
    assert _extract_next_data("<html></html>") is None


def test_find_ads_walks_nested_tree():
    data = _extract_next_data(SAMPLE_HTML)
    ads = _find_ads(data)
    ids = {a["list_id"] for a in ads}
    assert ids == {111, 222}


def test_get_attr_extracts_value():
    ad = {"attributes": [{"key": "square", "value": "70"}]}
    assert _get_attr(ad, "square") == "70"
    assert _get_attr(ad, "missing") is None


def test_to_listing_maps_price_list_and_attributes():
    data = _extract_next_data(SAMPLE_HTML)
    ads = {a["list_id"]: a for a in _find_ads(data)}
    listing = _to_listing(ads[111], "sale")
    assert listing.price == 500000
    assert listing.surface_m2 == 70
    assert listing.rooms == 3
    assert listing.has_elevator is True
    assert listing.postal_code == "75015"
    assert listing.validate() == []


def test_to_listing_maps_price_cents():
    data = _extract_next_data(SAMPLE_HTML)
    ads = {a["list_id"]: a for a in _find_ads(data)}
    listing = _to_listing(ads[222], "sale")
    assert listing.price == 610000.0


def test_to_listing_returns_none_without_list_id():
    assert _to_listing({"price": 100}, "sale") is None
