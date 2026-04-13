from poe2market.api import (
    build_search_query,
    extract_number,
    find_stats,
    resolve_stat,
)
from poe2market.models import StatFilter


def test_find_stats_basic(sample_stats_data):
    matches = find_stats("life", sample_stats_data)
    ids = [m[0] for m in matches]
    assert "pseudo.pseudo_total_life" in ids
    assert "explicit.stat_life" in ids


def test_find_stats_case_insensitive(sample_stats_data):
    matches = find_stats("FIRE", sample_stats_data)
    assert len(matches) >= 2  # pseudo + explicit


def test_find_stats_no_match(sample_stats_data):
    matches = find_stats("nonexistent_xyz", sample_stats_data)
    assert matches == []


def test_resolve_stat_prefers_pseudo(sample_stats_data):
    result = resolve_stat("maximum life", sample_stats_data, prefer_pseudo=True)
    assert result is not None
    assert result[0].startswith("pseudo.")


def test_resolve_stat_explicit(sample_stats_data):
    result = resolve_stat("spell damage", sample_stats_data)
    assert result is not None
    assert result[0] == "explicit.stat_spell_dmg"


def test_resolve_stat_no_match(sample_stats_data):
    result = resolve_stat("nonexistent_stat_xyz", sample_stats_data)
    assert result is None


def test_build_query_basic():
    q = build_search_query(category="armour.body")
    assert q["query"]["filters"]["type_filters"]["filters"]["category"]["option"] == "armour.body"
    assert q["sort"] == {"price": "asc"}


def test_build_query_price_range():
    q = build_search_query(category="weapon.staff", min_price=10, max_price=500)
    price = q["query"]["filters"]["trade_filters"]["filters"]["price"]
    assert price["min"] == 10
    assert price["max"] == 500
    assert price["option"] == "chaos"


def test_build_query_stat_filters():
    filters = [
        StatFilter(stat_id="explicit.stat_life", min_value=50),
        StatFilter(stat_id="explicit.stat_fire_res"),
    ]
    q = build_search_query(category="armour.body", stat_filters=filters)
    stats = q["query"]["stats"]
    assert len(stats) == 1
    group = stats[0]
    assert group["type"] == "count"
    assert group["value"]["min"] == 1
    assert len(group["filters"]) == 2


def test_build_query_online_flag():
    q_online = build_search_query(online_only=True)
    assert q_online["query"]["status"]["option"] == "online"

    q_any = build_search_query(online_only=False)
    assert q_any["query"]["status"]["option"] == "any"
