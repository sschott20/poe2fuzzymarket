from poe2market.api import extract_number
from poe2market.models import Deal, Listing, StatCoefficient, StatValue


def test_extract_plain_positive():
    assert extract_number("+95 to maximum Life") == 95.0


def test_extract_plain_negative():
    assert extract_number("-10 to Mana Cost of Skills") == -10.0


def test_extract_percentage():
    assert extract_number("+40% to Cold Resistance") == 40.0


def test_extract_decimal():
    assert extract_number("1.5% of Life Regenerated per Second") == 1.5


def test_extract_range():
    result = extract_number("Adds (10-20) to (30-40) Fire Damage")
    assert result == 15.0  # average of first range


def test_extract_no_number():
    assert extract_number("Cannot be Frozen") is None


def test_listing_defaults():
    listing = Listing(item_id="x", price=10, currency="chaos")
    assert listing.stats == []
    assert listing.name == ""
    assert listing.ilvl == 0


def test_stat_coefficient_defaults():
    c = StatCoefficient(stat_id="x", text="y", coefficient=1.5)
    assert c.std_error == 0.0
    assert c.sample_count == 0


def test_deal_defaults():
    listing = Listing(item_id="x", price=10, currency="chaos")
    deal = Deal(listing=listing, weighted_score=50, value_ratio=5.0)
    assert deal.stat_contributions == {}
