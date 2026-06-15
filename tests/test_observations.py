import tempfile

from poe2market.observations import ObservationStore, defence_flags


def _raw(item_id, base, price, currency, props=None, mods=None):
    return {
        "id": item_id,
        "listing": {"price": {"amount": price, "currency": currency}},
        "item": {
            "typeLine": base,
            "baseType": base,
            "ilvl": 80,
            "properties": props or [],
            "explicitMods": mods or [],
            "extended": {"hashes": {}},
        },
    }


def test_defence_flags():
    item = {"properties": [
        {"name": "[EnergyShield|Energy Shield]", "values": [["80", 1]]},
        {"name": "Evasion Rating", "values": [["120", 1]]},
    ]}
    flags = defence_flags(item)
    assert flags == {"ar": 0, "ev": 1, "es": 1}  # dex/int base


def test_record_and_count_dedup():
    store = ObservationStore(tempfile.mkdtemp())
    items = [
        _raw("a", "Bound Sandals", 5, "exalted"),
        _raw("b", "Bound Sandals", 9, "exalted"),
    ]
    assert store.record("L", "armour.boots", items, ts=1.0) == 2
    # Re-recording the same ids is not new, but refreshes (no dup rows).
    assert store.record("L", "armour.boots", items, ts=2.0) == 0
    assert store.count("L", "armour.boots") == 2
    assert store.count("L") == 2


def test_query_attribute_filter():
    store = ObservationStore(tempfile.mkdtemp())
    dexint = _raw("d1", "Bound Sandals", 5, "exalted", props=[
        {"name": "Energy Shield", "values": [["80", 1]]},
        {"name": "Evasion Rating", "values": [["120", 1]]},
    ])
    armour = _raw("s1", "Iron Greaves", 5, "exalted", props=[
        {"name": "Armour", "values": [["200", 1]]},
    ])
    store.record("L", "armour.boots", [dexint, armour], ts=1.0)

    dexint_only = store.query("L", "armour.boots", attributes=["dex", "int"])
    assert len(dexint_only) == 1
    assert dexint_only[0].base_type == "Bound Sandals"

    armour_only = store.query("L", "armour.boots", attributes=["str"])
    assert len(armour_only) == 1
    assert armour_only[0].base_type == "Iron Greaves"

    all_boots = store.query("L", "armour.boots")
    assert len(all_boots) == 2


def test_category_counts_and_clear():
    store = ObservationStore(tempfile.mkdtemp())
    store.record("L", "armour.boots", [_raw("a", "X", 1, "exalted")], ts=1.0)
    store.record("L", "armour.gloves", [_raw("b", "Y", 1, "exalted")], ts=1.0)
    counts = {c["category"]: c["count"] for c in store.category_counts("L")}
    assert counts == {"armour.boots": 1, "armour.gloves": 1}
    store.clear("L")
    assert store.count("L") == 0
