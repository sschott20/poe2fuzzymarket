"""Record price predictions and grade them against actual sales.

When the /appraise skill prices an item, it saves the full item SPEC plus the
guess here. Later, when that item shows up in the synced sale history, we match
it back two ways and grade predicted-vs-actual:

  1. exact stat FINGERPRINT (base + sockets + ms + life + res + chaos + rarity +
     corrupted) — works even when we don't have the item's name, and
  2. the unique rare NAME — a strong confirm when present.

This is a calibration loop: it shows whether the appraiser runs high, low, or
on target so the guesses can be corrected.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from .config import Config

# Grace before a prediction's timestamp during which a matching sale still counts
# (covers recording lag when a guess is logged shortly after the item sold).
GRACE_SECONDS = 3 * 86400


def fingerprint(spec: dict) -> str:
    """Exact, value-level stat fingerprint of a boot. Identical inputs on the
    prediction side and the sale side (both via this fn) produce the same key.
    'Runeforged' is stripped from the base so a runeforged item still matches."""
    base = (spec.get("base") or "").replace("Runeforged ", "").strip().lower()
    return "|".join(str(x) for x in [
        base, int(spec.get("sockets", 0)), int(spec.get("ms", 0)),
        int(spec.get("life", 0)), int(spec.get("res", 0)), int(spec.get("chaos_res", 0)),
        int(spec.get("rarity", 0)), int(bool(spec.get("corrupted", False)))])


def _spec_from_item(item: dict) -> dict:
    """Build the same spec shape from a fetched/sold item's raw JSON."""
    from .tracker import boot_attrs

    a = boot_attrs(item)
    return {"base": a.base, "defc": a.defc, "sockets": a.sockets, "ms": a.ms,
            "life": a.life, "res": a.res, "chaos_res": a.chaos_res, "rarity": a.rarity,
            "corrupted": a.corrupted}


class PredictionStore:
    def __init__(self, cache_dir: str):
        self.db_path = Path(cache_dir) / "predictions.db"
        Path(cache_dir).mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as c:
            c.execute("""
                CREATE TABLE IF NOT EXISTS predictions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts REAL, league TEXT, name TEXT, base TEXT, defc TEXT, sockets INT,
                    fingerprint TEXT, spec_json TEXT, low_d REAL, high_d REAL, note TEXT
                )""")
            c.execute("CREATE INDEX IF NOT EXISTS idx_pred_fp ON predictions (league, fingerprint)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_pred_name ON predictions (league, name)")

    def add(self, league: str, name: str, spec: dict, low_d: float, high_d: float,
            note: str = "", ts: float | None = None) -> None:
        with sqlite3.connect(self.db_path) as c:
            c.execute(
                "INSERT INTO predictions (ts, league, name, base, defc, sockets, fingerprint, "
                "spec_json, low_d, high_d, note) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (ts if ts is not None else time.time(), league, name, spec.get("base", ""),
                 spec.get("defc", ""), int(spec.get("sockets", 0)), fingerprint(spec),
                 json.dumps(spec), low_d, high_d, note))

    def all(self, league: str) -> list[dict]:
        with sqlite3.connect(self.db_path) as c:
            rows = c.execute(
                "SELECT ts, name, base, defc, sockets, fingerprint, spec_json, low_d, high_d, note "
                "FROM predictions WHERE league=? ORDER BY ts", (league,)).fetchall()
        cols = ["ts", "name", "base", "defc", "sockets", "fingerprint", "spec_json",
                "low_d", "high_d", "note"]
        return [dict(zip(cols, r)) for r in rows]


def _sold_boots(config: Config, div: float, rates: dict) -> tuple[dict, dict]:
    """Parse boots sales -> (by_name, by_fingerprint), each value {price_d, date, name}."""
    from .tracker import _price_ex

    path = Path(config.cache_dir) / "history.db"
    by_name: dict[str, dict] = {}
    by_fp: dict[str, dict] = {}
    if not path.exists():
        return by_name, by_fp
    with sqlite3.connect(path) as c:
        rows = c.execute(
            "SELECT time, amount, currency, name, raw FROM sales WHERE league=? AND "
            "(base_type LIKE '%Boot%' OR base_type LIKE '%Shoe%' OR base_type LIKE '%Sandal%' "
            "OR base_type LIKE '%Slipper%' OR base_type LIKE '%Greaves%' OR base_type LIKE '%Sabaton%')",
            (config.league,)).fetchall()
    import datetime

    def _ts(s: str) -> float:
        try:
            return datetime.datetime.fromisoformat((s or "").replace("Z", "+00:00")).timestamp()
        except ValueError:
            return 0.0

    for time_, amount, currency, name, raw in rows:
        try:
            item = json.loads(raw).get("item", {})
        except (ValueError, TypeError):
            item = {}
        rec = {"price_d": round(_price_ex(amount, currency, rates, div) / div, 2),
               "date": (time_ or "")[:10], "ts": _ts(time_), "name": name or ""}
        if name:
            k = name.strip().lower()
            if k not in by_name or rec["ts"] > by_name[k]["ts"]:
                by_name[k] = rec
        if item.get("explicitMods") or item.get("desecratedMods"):
            fp = fingerprint(_spec_from_item(item))
            if fp not in by_fp or rec["ts"] > by_fp[fp]["ts"]:
                by_fp[fp] = rec
    return by_name, by_fp


def compare(config: Config) -> dict:
    """Grade saved predictions against sales, matched by name then by fingerprint."""
    from .stash import fetch_exalted_rates

    rates, div = fetch_exalted_rates(league=config.league)
    by_name, by_fp = _sold_boots(config, div, rates)
    store = PredictionStore(config.cache_dir)
    graded, pending = [], []
    for p in store.all(config.league):
        # Exact stat fingerprint is the primary key (rare NAMES repeat in PoE);
        # name is a fallback. Only grade a sale near/after the prediction — a small
        # grace before it covers recording lag (appraise → sell → record), while
        # still excluding an unrelated older item with the same name/stats.
        cutoff = p["ts"] - GRACE_SECONDS
        fp_sale = by_fp.get(p["fingerprint"])
        nm_sale = by_name.get((p["name"] or "").strip().lower())
        sale, match_by = None, ""
        if fp_sale and fp_sale["ts"] >= cutoff:
            sale, match_by = fp_sale, "stats"
        elif nm_sale and nm_sale["ts"] >= cutoff:
            sale, match_by = nm_sale, "name"
        if sale is None:
            pending.append(p)
            continue
        actual = sale["price_d"]
        lo, hi = p["low_d"], p["high_d"]
        mid = (lo + hi) / 2 if hi else lo
        verdict = "OVER" if actual < lo else "UNDER" if (hi and actual > hi) else "in-range"
        err_pct = round(100 * (mid - actual) / actual) if actual else None
        graded.append({**p, "actual_d": actual, "sold_date": sale["date"],
                       "verdict": verdict, "err_pct": err_pct, "match_by": match_by})
    return {"graded": graded, "pending": pending,
            "n_graded": len(graded), "n_pending": len(pending)}


def print_report(config: Config) -> None:
    rep = compare(config)
    print(f"== appraisal accuracy [{config.league}] ==  "
          f"graded={rep['n_graded']}  pending={rep['n_pending']}")
    if rep["graded"]:
        print(f"\n{'item':<20} {'base':<20} {'predicted':>11} {'actual':>7} {'verdict':>9} {'err':>6} {'via':>5}")
        for g in sorted(rep["graded"], key=lambda x: x["sold_date"]):
            rng = f"{g['low_d']:.0f}-{g['high_d']:.0f}d"
            err = f"{g['err_pct']:+d}%" if g["err_pct"] is not None else ""
            print(f"{(g['name'] or '?')[:20]:<20} {g['base'][:20]:<20} {rng:>11} "
                  f"{g['actual_d']:>5.0f}d {g['verdict']:>9} {err:>6} {g['match_by']:>5}")
        errs = [g["err_pct"] for g in rep["graded"] if g["err_pct"] is not None]
        if errs:
            import statistics as st
            in_range = sum(1 for g in rep["graded"] if g["verdict"] == "in-range")
            print(f"\nmedian error: {st.median(errs):+.0f}%  (in-range {in_range}/{len(rep['graded'])})")
    if rep["pending"]:
        print(f"\npending (not sold yet): "
              + ", ".join(f"{p['name'] or p['base']} ({p['low_d']:.0f}-{p['high_d']:.0f}d)"
                          for p in rep["pending"]))


if __name__ == "__main__":
    print_report(Config.load())
