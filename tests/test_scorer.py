from poe2market.models import Listing, StatValue
from poe2market.scorer import normalize_price, score_listings


def test_normalize_chaos():
    assert normalize_price(10, "chaos") == 10.0


def test_normalize_divine():
    assert normalize_price(1, "divine") == 150.0


def test_normalize_unknown_currency():
    # Unknown currencies are treated as 1:1 with chaos
    assert normalize_price(5, "mirror") == 5.0


def test_normalize_custom_rates():
    rates = {"divine": 200.0}
    assert normalize_price(2, "divine", rates) == 400.0


def test_score_listings_basic(sample_listings):
    weights = {"explicit.stat_life": 2.0, "explicit.stat_fire_res": 1.0}
    deals = score_listings(sample_listings, weights)

    assert len(deals) > 0
    # Should be sorted by value_ratio descending
    for i in range(len(deals) - 1):
        assert deals[i].value_ratio >= deals[i + 1].value_ratio


def test_score_listings_contributions(sample_listings):
    weights = {"explicit.stat_life": 1.0}
    deals = score_listings(sample_listings, weights)

    for deal in deals:
        assert "explicit.stat_life" in deal.stat_contributions
        # Contribution should equal stat_value * weight
        life_stat = next(
            sv for sv in deal.listing.stats if sv.stat_id == "explicit.stat_life"
        )
        assert deal.stat_contributions["explicit.stat_life"] == life_stat.value * 1.0


def test_score_listings_no_matching_stats():
    listings = [
        Listing(
            item_id="x",
            price=10,
            currency="chaos",
            stats=[StatValue("unrelated", "test", 50)],
        )
    ]
    deals = score_listings(listings, {"other_stat": 1.0})
    assert deals == []


def test_score_listings_zero_price():
    """Items with zero price should be filtered out."""
    listings = [
        Listing(
            item_id="x",
            price=0,
            currency="chaos",
            stats=[StatValue("s1", "test", 50)],
        )
    ]
    deals = score_listings(listings, {"s1": 1.0})
    assert deals == []


def test_score_listings_mixed_currency():
    listings = [
        Listing(
            item_id="cheap_divine",
            price=1,
            currency="divine",
            stats=[StatValue("s1", "+100 Life", 100)],
        ),
        Listing(
            item_id="cheap_chaos",
            price=50,
            currency="chaos",
            stats=[StatValue("s1", "+100 Life", 100)],
        ),
    ]
    deals = score_listings(listings, {"s1": 1.0})
    assert len(deals) == 2
    # The chaos-priced one should be the better deal (100/50=2 vs 100/150≈0.67)
    assert deals[0].listing.item_id == "cheap_chaos"
