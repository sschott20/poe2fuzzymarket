import json
import sqlite3
import tempfile
from pathlib import Path

from poe2market.config import Config
from poe2market.predictions import PredictionStore, compare, fingerprint


def _spec(base, sockets, ms, life, res, rarity, chaos=0, corrupted=False):
    return {"base": base, "defc": "EV", "sockets": sockets, "ms": ms, "life": life,
            "res": res, "chaos_res": chaos, "rarity": rarity, "corrupted": corrupted}


SALE_TIME = "2026-06-15T00:00:00Z"   # sales happen "after" the predictions below
PRED_TS = 1000.0                      # predictions made long before the sale


def _seed_sale(cache_dir, league, amount, *, name="", base="Cinched Boots",
               item=None, currency="divine", time=SALE_TIME):
    """Insert a boots sale; item is the raw JSON dict the appraiser fingerprints."""
    db = Path(cache_dir) / "history.db"
    new = not db.exists()
    with sqlite3.connect(db) as c:
        if new:
            c.execute("CREATE TABLE sales (league TEXT, time TEXT, amount REAL, "
                      "currency TEXT, base_type TEXT, name TEXT, raw TEXT)")
        c.execute("INSERT INTO sales VALUES (?,?,?,?,?,?,?)",
                  (league, time, amount, currency, base, name,
                   json.dumps({"item": item or {}})))


def _item(base="Cinched Boots", ms=35, life=0, res=87, rarity=18, sockets=1):
    return {"typeLine": base,
            "properties": [{"name": "[Evasion|Evasion Rating]"}],
            "explicitMods": [f"{ms}% increased Movement Speed",
                             f"+{res//2}% to Fire Resistance", f"+{res-res//2}% to Cold Resistance"]
                            + ([f"+{life} to maximum Life"] if life else []),
            "craftedMods": [f"{rarity}% increased Rarity of Items found"],
            "sockets": [{}] * sockets}


def test_grades_in_range_over_under():
    d = tempfile.mkdtemp()
    cfg = Config(cache_dir=d, league="L")
    s = PredictionStore(d)
    s.add("L", "Alpha", _spec("Cinched Boots", 2, 35, 108, 80, 0), 60, 80, ts=PRED_TS)  # sells 70 -> in-range
    s.add("L", "Beta", _spec("Sekhema Sandals", 1, 35, 0, 50, 17), 20, 30, ts=PRED_TS)  # sells 5  -> OVER
    s.add("L", "Gamma", _spec("Daggerfoot Shoes", 2, 35, 0, 100, 18), 3, 5, ts=PRED_TS)  # sells 40 -> UNDER
    s.add("L", "Delta", _spec("Cavalry Boots", 1, 35, 0, 50, 15), 5, 8, ts=PRED_TS)      # not sold -> pending
    _seed_sale(d, "L", 70, name="Alpha")
    _seed_sale(d, "L", 5, name="Beta")
    _seed_sale(d, "L", 40, name="Gamma")

    rep = compare(cfg)
    assert rep["n_graded"] == 3 and rep["n_pending"] == 1
    by = {g["name"]: g for g in rep["graded"]}
    assert by["Alpha"]["verdict"] == "in-range"
    assert by["Beta"]["verdict"] == "OVER"
    assert by["Gamma"]["verdict"] == "UNDER"
    assert rep["pending"][0]["name"] == "Delta"


def test_matches_by_exact_stats_when_name_missing():
    """The whole point: grade a sale even when we never had its name."""
    d = tempfile.mkdtemp()
    cfg = Config(cache_dir=d, league="L")
    spec = _spec("Serpentscale Boots", 1, 35, 0, 87, 18)
    PredictionStore(d).add("L", "", spec, 5, 8, ts=PRED_TS)   # no name stored
    # sale also has no usable name; must match on stat fingerprint
    _seed_sale(d, "L", 6, name="", base="Serpentscale Boots",
               item=_item("Serpentscale Boots", ms=35, life=0, res=87, rarity=18, sockets=1))
    rep = compare(cfg)
    assert rep["n_graded"] == 1
    g = rep["graded"][0]
    assert g["verdict"] == "in-range" and g["match_by"] == "stats"


def test_ignores_sale_made_before_the_prediction():
    """A same-name/same-stats item that sold BEFORE we predicted must not match."""
    import datetime

    d = tempfile.mkdtemp()
    cfg = Config(cache_dir=d, league="L")
    spec = _spec("Cinched Boots", 1, 35, 0, 41, 17, chaos=25)
    pred_ts = datetime.datetime.fromisoformat("2026-06-14T00:00:00+00:00").timestamp()
    PredictionStore(d).add("L", "Plague Urge", spec, 3, 6, ts=pred_ts)
    # the only matching sale happened well before the prediction
    _seed_sale(d, "L", 7, name="Plague Urge", base="Cinched Boots",
               item=_item("Cinched Boots", ms=35, life=0, res=41, rarity=17, sockets=1),
               time="2026-06-10T00:00:00Z")   # before pred_ts
    rep = compare(cfg)
    assert rep["n_graded"] == 0 and rep["n_pending"] == 1


def test_runeforged_base_still_fingerprint_matches():
    spec = _spec("Wanderer Shoes", 1, 35, 0, 70, 16)
    sale_item = _item("Runeforged Wanderer Shoes", ms=35, life=0, res=70, rarity=16, sockets=1)
    from poe2market.predictions import _spec_from_item
    assert fingerprint(spec) == fingerprint(_spec_from_item(sale_item))
