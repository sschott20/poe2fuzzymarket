import tempfile

import pytest

from poe2market.stash import (
    NetWorth,
    NetWorthStore,
    parse_stack_size,
    value_currency,
)


def test_parse_stack_size_from_field():
    assert parse_stack_size({"stackSize": 742}) == 742


def test_parse_stack_size_from_property():
    item = {
        "properties": [
            {"name": "Stack Size", "values": [["1,234/5000", 0]]},
        ]
    }
    assert parse_stack_size(item) == 1234


def test_parse_stack_size_non_stackable_defaults_one():
    assert parse_stack_size({"baseType": "Some Ring"}) == 1


def test_value_currency_basic():
    items = [
        {"baseType": "Divine Orb", "stackSize": 3, "icon": "div.png"},
        {"baseType": "Exalted Orb", "stackSize": 100},
        {"baseType": "Chaos Orb", "stackSize": 50},
        {"baseType": "Mystery Thing", "stackSize": 5},  # unpriced
    ]
    price_map = {
        "divine orb": {"price": 124.0, "icon": ""},
        "exalted orb": {"price": 1.0, "icon": ""},
        "chaos orb": {"price": 12.0, "icon": ""},
    }
    holdings, unpriced = value_currency(items, price_map, divine_price=124.0)

    # 3*124 + 100*1 + 50*12 = 372 + 100 + 600 = 1072 exalted
    total = sum(h.total_ex for h in holdings)
    assert total == pytest.approx(1072.0)
    # Sorted by exalted value descending: chaos(600) > divine(372) > exalted(100)
    assert [h.name for h in holdings] == ["Chaos Orb", "Divine Orb", "Exalted Orb"]
    # Item icon from stash wins over price-map icon.
    div = next(h for h in holdings if h.name == "Divine Orb")
    assert div.icon == "div.png"
    # Unpriced item surfaced, not silently dropped.
    assert unpriced == [{"name": "Mystery Thing", "quantity": 5}]


def test_value_currency_aggregates_duplicates():
    items = [
        {"baseType": "Exalted Orb", "stackSize": 100},
        {"baseType": "Exalted Orb", "stackSize": 250},  # second stack
    ]
    price_map = {"exalted orb": {"price": 1.0, "icon": ""}}
    holdings, _ = value_currency(items, price_map, divine_price=124.0)
    assert len(holdings) == 1
    assert holdings[0].quantity == 350


def test_networth_to_dict_shares_and_divine():
    nw = NetWorth(
        league="Runes of Aldur",
        total_exalted=1000.0,
        total_divine=1000.0 / 125.0,
        divine_price=125.0,
        holdings=[
            __import__("poe2market.stash", fromlist=["Holding"]).Holding(
                name="Chaos Orb", quantity=50, unit_price_ex=12.0
            )
        ],
    )
    d = nw.to_dict()
    assert d["total_divine"] == pytest.approx(8.0)
    h = d["holdings"][0]
    assert h["total_ex"] == pytest.approx(600.0)
    assert h["total_div"] == pytest.approx(600.0 / 125.0)
    assert h["share"] == pytest.approx(0.6)


def test_networth_store_roundtrip():
    tmp = tempfile.mkdtemp()
    store = NetWorthStore(tmp)
    nw1 = NetWorth(league="Runes of Aldur", total_exalted=500, total_divine=4, divine_price=125)
    nw2 = NetWorth(league="Runes of Aldur", total_exalted=800, total_divine=6.4, divine_price=125)
    store.add(nw1, ts=1000.0)
    store.add(nw2, ts=2000.0)

    series = store.series("Runes of Aldur")
    assert [s["exalted"] for s in series] == [500, 800]

    latest = store.latest("Runes of Aldur")
    assert latest["total_exalted"] == 800
    assert latest["ts"] == 2000.0

    store.clear("Runes of Aldur")
    assert store.series("Runes of Aldur") == []
