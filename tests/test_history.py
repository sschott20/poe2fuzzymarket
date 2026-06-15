import tempfile

import pytest

from poe2market.history import (
    HistoryStore,
    Sale,
    clean_markup,
    parse_sale,
    summarize,
)


def test_clean_markup():
    assert clean_markup("+38% to [Resistances|Fire Resistance]") == "+38% to Fire Resistance"
    assert clean_markup("[Corrupted]") == "Corrupted"
    assert clean_markup("plain text") == "plain text"


def test_parse_sale_extracts_mods():
    raw = {
        "time": "2026-06-11T14:27:22Z",
        "item_id": "boots1",
        "price": {"amount": 4, "currency": "exalted"},
        "item": {
            "name": "Corruption Pace",
            "baseType": "Bound Sandals",
            "rarity": "Rare",
            "ilvl": 82,
            "explicitMods": [
                "30% increased Movement Speed",
                "+38% to [Resistances|Fire Resistance]",
            ],
            "implicitMods": ["+10 to [Strength|Strength]"],
            "properties": [
                {"name": "[EnergyShield|Energy Shield]", "values": [["93", 1]]}
            ],
        },
    }
    sale = parse_sale(raw)
    assert sale.explicit_mods == [
        "30% increased Movement Speed",
        "+38% to Fire Resistance",
    ]
    assert sale.implicit_mods == ["+10 to Strength"]
    assert sale.properties == ["Energy Shield: 93"]


# Sample raw entries modeled on the /api/trade2/history/{league} response,
# using the real items the user recently sold.
RAW_ENTRIES = [
    {
        "time": "2026-06-11T09:20:00Z",
        "item_id": "sale1",
        "price": {"amount": 9, "currency": "chaos"},
        "item": {
            "name": "Voidtouched Judgement",
            "baseType": "Abyss Tablet",
            "typeLine": "Abyss Tablet",
            "rarity": "Rare",
            "ilvl": 80,
            "icon": "https://web.poecdn.com/abyss.png",
        },
    },
    {
        "time": "2026-06-11T08:00:00Z",
        "item_id": "sale2",
        "price": {"amount": 1, "currency": "divine"},
        "item": {
            "name": "Entropy Urge",
            "baseType": "Cinched Boots",
            "rarity": "Rare",
            "ilvl": 78,
        },
    },
    {
        "time": "2026-06-10T22:00:00Z",
        "item_id": "sale3",
        "price": {"amount": 2, "currency": "divine"},
        "item": {
            "name": "Heart of the Well",
            "baseType": "Diamond",
            "rarity": "Unique",
            "ilvl": 0,
        },
    },
    {
        "time": "2026-06-10T20:00:00Z",
        "item_id": "sale4",
        "price": {"amount": 28, "currency": "divine"},
        "item": {
            "name": "Brood League",
            "baseType": "Serpentscale Boots",
            "rarity": "Rare",
            "ilvl": 82,
        },
    },
]


def test_parse_sale_flat_shape():
    sale = parse_sale(RAW_ENTRIES[0])
    assert sale.sale_id == "sale1"
    assert sale.amount == 9
    assert sale.currency == "chaos"
    assert sale.name == "Voidtouched Judgement"
    assert sale.base_type == "Abyss Tablet"
    assert sale.rarity == "Rare"
    assert sale.ilvl == 80


def test_parse_sale_listing_shape():
    raw = {
        "id": "x9",
        "listing": {
            "indexed": "2026-06-01T00:00:00Z",
            "price": {"amount": 5, "currency": "exalted"},
        },
        "item": {"name": "", "typeLine": "Sapphire Ring", "rarity": "Magic"},
    }
    sale = parse_sale(raw)
    assert sale.sale_id == "x9"
    assert sale.time == "2026-06-01T00:00:00Z"
    assert sale.amount == 5
    assert sale.currency == "exalted"
    assert sale.base_type == "Sapphire Ring"


def test_parse_sale_synthesizes_key_when_no_id():
    raw = {
        "time": "2026-06-01T00:00:00Z",
        "price": {"amount": 3, "currency": "chaos"},
        "item": {"name": "Foo", "typeLine": "Bar"},
    }
    sale = parse_sale(raw)
    assert sale.sale_id.startswith("syn:")


def test_exalted_value():
    sale = Sale(sale_id="x", amount=2, currency="divine")
    rates = {"exalted": 1.0, "divine": 124.0}
    assert sale.exalted_value(rates) == 248  # 2 divine * 124 ex


def _store():
    tmp = tempfile.mkdtemp()
    return HistoryStore(tmp)


def test_store_upsert_dedupes():
    store = _store()
    added = store.upsert_many("Runes of Aldur", RAW_ENTRIES)
    assert added == 4
    # Re-syncing the same entries adds nothing.
    again = store.upsert_many("Runes of Aldur", RAW_ENTRIES)
    assert again == 0
    assert store.count("Runes of Aldur") == 4


def test_store_partitions_by_league():
    store = _store()
    store.upsert_many("Runes of Aldur", RAW_ENTRIES[:2])
    store.upsert_many("Standard", RAW_ENTRIES[2:])
    assert store.count("Runes of Aldur") == 2
    assert store.count("Standard") == 2
    assert set(store.leagues()) == {"Runes of Aldur", "Standard"}


def test_store_all_sorted_newest_first():
    store = _store()
    store.upsert_many("Runes of Aldur", RAW_ENTRIES)
    sales = store.all("Runes of Aldur")
    times = [s.time for s in sales]
    assert times == sorted(times, reverse=True)


def test_store_clear():
    store = _store()
    store.upsert_many("Runes of Aldur", RAW_ENTRIES)
    store.clear("Runes of Aldur")
    assert store.count("Runes of Aldur") == 0


RATES = {"exalted": 1.0, "divine": 124.0, "chaos": 12.0}


def test_summarize_totals():
    sales = [parse_sale(r) for r in RAW_ENTRIES]
    summary = summarize(sales, RATES)
    # 9 chaos + 31 divine = 9*12 + 31*124 = 108 + 3844 = 3952 exalted
    assert summary.count == 4
    assert summary.total_exalted == pytest.approx(9 * 12 + 31 * 124)
    assert summary.total_divine == pytest.approx(summary.total_exalted / 124)
    assert summary.divine_price == pytest.approx(124)
    # Biggest sale is the 28 divine boots.
    assert summary.max_sale_exalted == pytest.approx(28 * 124)
    assert "Brood League" in summary.max_sale_label


def test_summarize_series_present():
    sales = [parse_sale(r) for r in RAW_ENTRIES]
    summary = summarize(sales, RATES)
    # Cumulative is monotonically non-decreasing.
    cum = [p["exalted"] for p in summary.cumulative]
    assert cum == sorted(cum)
    assert cum[-1] == pytest.approx(summary.total_exalted)
    # Daily buckets cover the two distinct dates.
    days = {d["date"] for d in summary.daily}
    assert days == {"2026-06-10", "2026-06-11"}
    # Currency breakdown sums back to the total.
    assert sum(c["exalted"] for c in summary.by_currency) == pytest.approx(summary.total_exalted)


def test_summarize_empty():
    summary = summarize([])
    assert summary.count == 0
    assert summary.total_exalted == 0
    assert summary.cumulative == []
