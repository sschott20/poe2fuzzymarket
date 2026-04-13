import pytest

from poe2market.models import Listing, StatValue


@pytest.fixture
def sample_listings() -> list[Listing]:
    """A small set of listings with known stats for testing regression/scoring."""
    return [
        Listing(
            item_id="a1",
            price=50,
            currency="chaos",
            stats=[
                StatValue("explicit.stat_life", "+90 to maximum Life", 90),
                StatValue("explicit.stat_fire_res", "+35% to Fire Resistance", 35),
            ],
            name="",
            base_type="Plate Vest",
            ilvl=80,
        ),
        Listing(
            item_id="a2",
            price=120,
            currency="chaos",
            stats=[
                StatValue("explicit.stat_life", "+145 to maximum Life", 145),
                StatValue("explicit.stat_fire_res", "+40% to Fire Resistance", 40),
            ],
            name="",
            base_type="Full Plate",
            ilvl=84,
        ),
        Listing(
            item_id="a3",
            price=30,
            currency="chaos",
            stats=[
                StatValue("explicit.stat_fire_res", "+45% to Fire Resistance", 45),
                StatValue("explicit.stat_cold_res", "+30% to Cold Resistance", 30),
            ],
            name="",
            base_type="Plate Vest",
            ilvl=75,
        ),
        Listing(
            item_id="a4",
            price=200,
            currency="chaos",
            stats=[
                StatValue("explicit.stat_life", "+160 to maximum Life", 160),
                StatValue("explicit.stat_fire_res", "+42% to Fire Resistance", 42),
                StatValue("explicit.stat_cold_res", "+38% to Cold Resistance", 38),
            ],
            name="",
            base_type="Full Plate",
            ilvl=86,
        ),
        Listing(
            item_id="a5",
            price=80,
            currency="chaos",
            stats=[
                StatValue("explicit.stat_life", "+110 to maximum Life", 110),
                StatValue("explicit.stat_cold_res", "+40% to Cold Resistance", 40),
            ],
            name="",
            base_type="Plate Vest",
            ilvl=82,
        ),
    ]


@pytest.fixture
def sample_stats_data() -> list[dict]:
    """Minimal stats data matching the stats endpoint format."""
    return [
        {
            "id": "pseudo",
            "label": "Pseudo",
            "entries": [
                {"id": "pseudo.pseudo_total_life", "text": "+# to maximum Life", "type": "pseudo"},
                {"id": "pseudo.pseudo_total_fire_resistance", "text": "+#% to Fire Resistance", "type": "pseudo"},
            ],
        },
        {
            "id": "explicit",
            "label": "Explicit",
            "entries": [
                {"id": "explicit.stat_life", "text": "+# to maximum Life", "type": "explicit"},
                {"id": "explicit.stat_fire_res", "text": "+#% to Fire Resistance", "type": "explicit"},
                {"id": "explicit.stat_cold_res", "text": "+#% to Cold Resistance", "type": "explicit"},
                {"id": "explicit.stat_spell_dmg", "text": "#% increased Spell Damage", "type": "explicit"},
            ],
        },
    ]
