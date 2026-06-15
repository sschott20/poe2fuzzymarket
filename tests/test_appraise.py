import json
import sqlite3
import tempfile
from pathlib import Path

from poe2market.appraise import _attrs_from_spec, comparable_sales, tracker_comps
from poe2market.config import Config


SPEC = {
    "base": "Cinched Boots", "defc": "EV", "sockets": 2, "ilvl": 84, "corrupted": False,
    "ms": 35, "life": 108, "res": 92, "chaos_res": 0, "rarity": 0,
    "open_prefix": 0, "open_suffix": 0, "rarity_kind": "rare",
}


def test_spec_mod_sig_matches_tracker_bucketing():
    a = _attrs_from_spec(SPEC)
    assert a.mod_sig == "EV|2s|ms35|res70-99|life80-119|rarN"


def _seed_history(cache_dir, league="L"):
    """Minimal history.db with one matching boot sale."""
    db = Path(cache_dir) / "history.db"
    item = {
        "typeLine": "Cinched Boots",
        "properties": [{"name": "Boots"}, {"name": "[Evasion|Evasion Rating]"}],
        "explicitMods": ["35% increased Movement Speed", "+108 to maximum Life",
                         "+43% to Lightning Resistance", "83% increased Evasion Rating"],
        "craftedMods": ["+35% to Fire Resistance"], "sockets": [{}, {}],
    }
    with sqlite3.connect(db) as c:
        c.execute("CREATE TABLE sales (league TEXT, time TEXT, amount REAL, currency TEXT, "
                  "base_type TEXT, raw TEXT)")
        c.execute("INSERT INTO sales VALUES (?,?,?,?,?,?)",
                  (league, "2026-06-09T00:00:00Z", 100.0, "divine", "Cinched Boots",
                   json.dumps({"item": item})))


def test_comparable_sales_finds_match(monkeypatch):
    d = tempfile.mkdtemp()
    _seed_history(d, "L")
    cfg = Config(cache_dir=d, league="L")
    # the seeded boot is res=43+35=78 -> res70-99, life108 -> 80-119 : same bucket as SPEC
    out = comparable_sales(cfg, SPEC, div=130.0, rates={"divine": 130.0, "exalted": 1.0})
    found = out["exact"] + out["similar"]
    assert any(abs(r["price_d"] - 100.0) < 0.01 and r["base"] == "Cinched Boots" for r in found)


def test_tracker_comps_handles_empty_db():
    d = tempfile.mkdtemp()
    cfg = Config(cache_dir=d, league="L")
    out = tracker_comps(cfg, SPEC, div=130.0)
    assert out["bucket"] is None and out["n_active"] == 0


def test_attrs_defaults_safe_on_sparse_spec():
    a = _attrs_from_spec({"base": "Sandsworn Sandals", "defc": "ES"})
    assert a.sockets == 0 and a.ms == 0 and a.signature().startswith("ES|0s|ms<30")
