from pipeline.scrapers.base import Listing


def test_valid_listing_has_no_problems():
    listing = Listing(
        source="bienici", source_id="1", url="http://x", transaction_type="sale",
        price=300000, surface_m2=50,
    )
    assert listing.validate() == []


def test_missing_source_id_flagged():
    listing = Listing(source="bienici", source_id="", url="http://x", transaction_type="sale")
    assert "missing source_id" in listing.validate()


def test_invalid_transaction_type_flagged():
    listing = Listing(source="bienici", source_id="1", url="http://x", transaction_type="lease")
    problems = listing.validate()
    assert any("invalid transaction_type" in p for p in problems)


def test_negative_price_flagged():
    listing = Listing(source="bienici", source_id="1", url="http://x", transaction_type="sale", price=-100)
    assert "negative price" in listing.validate()


def test_non_positive_surface_flagged():
    listing = Listing(source="bienici", source_id="1", url="http://x", transaction_type="sale", surface_m2=0)
    assert "non-positive surface_m2" in listing.validate()


def test_price_per_m2_none_without_surface():
    listing = Listing(source="bienici", source_id="1", url="http://x", transaction_type="sale", price=100000)
    assert listing.price_per_m2 is None


def test_price_per_m2_computed_correctly():
    listing = Listing(
        source="bienici", source_id="1", url="http://x", transaction_type="sale",
        price=200000, surface_m2=40,
    )
    assert listing.price_per_m2 == 5000.0
