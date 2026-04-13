from poe2market.analyzer import fit_price_model, get_common_stats
from poe2market.models import Listing, StatValue


def test_get_common_stats(sample_listings):
    stats = get_common_stats(sample_listings, min_fraction=0.5)
    # life appears in 4/5, fire_res in 4/5, cold_res in 3/5
    assert "explicit.stat_life" in stats
    assert "explicit.stat_fire_res" in stats
    assert "explicit.stat_cold_res" in stats


def test_get_common_stats_high_threshold(sample_listings):
    stats = get_common_stats(sample_listings, min_fraction=0.9)
    # threshold = int(5 * 0.9) = 4; life and fire_res appear in 4/5
    assert "explicit.stat_life" in stats
    assert "explicit.stat_fire_res" in stats
    assert "explicit.stat_cold_res" not in stats  # only 3/5


def test_fit_price_model_basic(sample_listings):
    coefficients = fit_price_model(sample_listings, min_occurrence=0.3)
    assert len(coefficients) > 0

    coeff_map = {c.stat_id: c for c in coefficients}

    # Life should have a positive coefficient (more life = higher price)
    if "explicit.stat_life" in coeff_map:
        assert coeff_map["explicit.stat_life"].coefficient > 0


def test_fit_price_model_empty():
    result = fit_price_model([], min_occurrence=0.1)
    assert result == []


def test_fit_price_model_single_stat():
    listings = [
        Listing(
            item_id="x",
            price=float(i * 10),
            currency="chaos",
            stats=[StatValue("s1", "test", float(i * 10))],
        )
        for i in range(1, 11)
    ]
    coefficients = fit_price_model(listings, stat_ids=["s1"])
    assert len(coefficients) == 1
    # Price = stat_value, so coefficient should be ~1.0
    assert abs(coefficients[0].coefficient - 1.0) < 0.1


def test_fit_price_model_sample_counts(sample_listings):
    coefficients = fit_price_model(sample_listings, min_occurrence=0.3)
    for c in coefficients:
        assert c.sample_count > 0
