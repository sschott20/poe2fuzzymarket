"""Boots appraisal engine for the /appraise skill.

The model parses the pasted in-game item text into a structured spec and calls
this engine, which gathers the EVIDENCE needed to price it and advise crafting:

  * comparable PAST sales from the user's own history.db (ground truth)
  * comparable CLEARING prices + active listings from the tracker.db
  * fresh OFFLINE comparable listings from a live trade search

The engine is deterministic data-plumbing; the model does the judgement (price
range, rune pick, desecration/exalt advice) using these findings plus the
research in memory. Prices are returned in divine.

Item spec (JSON) the model passes in:
  {
    "base": "Cinched Boots", "defc": "EV",        # EV / EV+ES / ES / AR / ...
    "sockets": 2, "ilvl": 84, "corrupted": false,
    "ms": 35, "life": 108, "res": 92, "chaos_res": 0, "rarity": 0,
    "open_prefix": 0, "open_suffix": 0,           # for craft advice
    "rarity_kind": "rare"                          # rare | magic | normal
  }
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

from .config import Config
from .tracker import (
    BootAttrs, LIFE, MS, P_RES, PSEU_MS, RARITY, TrackerStore, _price_ex, boot_attrs,
)


def _attrs_from_spec(s: dict) -> BootAttrs:
    ms = int(s.get("ms", 0))
    ms_explicit = int(s.get("ms_explicit", ms))
    a = BootAttrs(
        base=s.get("base", ""), defc=s.get("defc", "?"), sockets=int(s.get("sockets", 0)),
        ms=ms, life=int(s.get("life", 0)), res=int(s.get("res", 0)),
        rarity=int(s.get("rarity", 0)), corrupted=bool(s.get("corrupted", False)),
        runeforged=bool(s.get("runeforged", False)),
        ms_explicit=ms_explicit, ms_rune=bool(s.get("ms_rune", ms_explicit < ms)),
        chaos_res=int(s.get("chaos_res", 0)),
    )
    a.mod_sig = a.signature()
    return a


def comparable_sales(cfg: Config, spec: dict, div: float, rates: dict) -> dict:
    """Your OWN sold boots, ranked by similarity to the item (ground truth)."""
    target = _attrs_from_spec(spec)
    path = Path(cfg.cache_dir) / "history.db"
    if not path.exists():
        return {"exact": [], "similar": [], "same_defence": []}
    out = {"exact": [], "similar": [], "same_defence": []}
    with sqlite3.connect(path) as c:
        rows = c.execute(
            "SELECT time, amount, currency, base_type, raw FROM sales WHERE league=? AND "
            "(base_type LIKE '%Boot%' OR base_type LIKE '%Shoe%' OR base_type LIKE '%Sandal%' "
            "OR base_type LIKE '%Slipper%' OR base_type LIKE '%Greaves%' OR base_type LIKE '%Sabaton%')",
            (cfg.league,)).fetchall()
    for time_, amount, currency, base_type, raw in rows:
        try:
            item = json.loads(raw).get("item", {})
        except (ValueError, TypeError):
            continue
        if not item.get("explicitMods"):
            continue
        a = boot_attrs(item)
        rec = {
            "price_d": round(_price_ex(amount, currency, rates, div) / div, 2),
            "date": (time_ or "")[:10], "base": a.base, "defc": a.defc, "sockets": a.sockets,
            "ms": a.ms, "life": a.life, "res": a.res, "rarity": a.rarity, "mod_sig": a.mod_sig,
        }
        if a.mod_sig == target.mod_sig:
            out["exact"].append(rec)
        elif a.defc == target.defc and a.sockets == target.sockets and abs(a.ms - target.ms) <= 5:
            out["similar"].append(rec)
        elif a.defc == target.defc:
            out["same_defence"].append(rec)
    for k in out:
        out[k].sort(key=lambda r: -r["price_d"])
    return out


def tracker_comps(cfg: Config, spec: dict, div: float) -> dict:
    """Clearing-price bucket + active comparable listings from the tracker."""
    target = _attrs_from_spec(spec)
    store = TrackerStore(cfg.cache_dir)
    clearing = [r for r in store.clearing_report(cfg.league, defence=None)
                if r["mod_sig"] == target.mod_sig]
    bucket = None
    if clearing:
        r = clearing[0]
        bucket = {
            "mod_sig": r["mod_sig"], "n_sold": r["n_exits"],
            "exit_median_d": round(r["median_exit_ex"] / div, 2),
            "p25_d": round(r["p25_exit_ex"] / div, 2), "p75_d": round(r["p75_exit_ex"] / div, 2),
            "median_days_on_market": r["median_days_on_market"],
            "reprice_down_rate": r["reprice_down_rate"],
        }
    active = []
    for it in store.all_listings(cfg.league):
        a = BootAttrs(base=it["base"], defc=it["defc"], sockets=it["sockets"], ms=it["ms"],
                      life=it["life"], res=it["res"], rarity=it["rarity"],
                      corrupted=bool(it["corrupted"]), runeforged=bool(it["runeforged"]),
                      ms_explicit=it["ms_explicit"], ms_rune=it["ms"] > it["ms_explicit"],
                      chaos_res=it["chaos_res"])
        if a.signature() != target.mod_sig:
            continue
        active.append({"base": it["base"], "price_d": round(it["last_price_ex"] / div, 2),
                       "ms": it["ms"], "life": it["life"], "res": it["res"],
                       "rarity": it["rarity"], "sockets": it["sockets"], "status": it["status"]})
    active.sort(key=lambda r: r["price_d"])
    return {"bucket": bucket, "active_comps": active[:15], "n_active": len(active)}


def live_comparables(cfg: Config, spec: dict, div: float, rates: dict, max_fetch: int = 60,
                     max_div: float = 200.0) -> dict:
    """Fresh OFFLINE listings matching the item's key mods, as a price sanity check.

    Capped at ``max_div`` so mirror-fishing listings don't skew the estimate high —
    the user rarely transacts above this bracket.
    """
    import statistics as st

    from .api import TradeAPI

    target = _attrs_from_spec(spec)
    # Tight match so the offline FLOOR is a comparable lower bound (a loose filter
    # just returns 1d junk). Within ~one bucket of the item on each axis.
    stats = [(MS, max(30, target.ms - 1), None)]
    if target.res >= 40:
        stats.append((P_RES, max(40, target.res - 8), None))
    if target.life >= 80:
        stats.append((LIFE, target.life - 12, None))
    if target.rarity >= 10:
        stats.append((RARITY, max(10, target.rarity - 3), None))
    sfilters = [{"id": s, "value": ({"min": mn} if mx is None else {"min": mn, "max": mx}),
                 "disabled": False} for s, mn, mx in stats]
    filters = {"type_filters": {"filters": {"category": {"option": "armour.boots"},
                                            "rarity": {"option": "rare"}, "ilvl": {"min": 79}}},
               "trade_filters": {"filters": {"price": {"max": max_div, "option": "divine"}}}}
    if target.sockets >= 2:
        filters["equipment_filters"] = {"filters": {"rune_sockets": {"min": 2}}}
    elif target.sockets <= 1:
        filters["equipment_filters"] = {"filters": {"rune_sockets": {"max": 1}}}
    query = {"query": {"status": {"option": "any"}, "stats": [{"type": "and", "filters": sfilters}],
                       "filters": filters}, "sort": {"price": "asc"}}
    api = TradeAPI(cfg)
    try:
        qid, ids, total = api.search(query)
        listings = api.fetch(ids[:max_fetch], qid) if ids else []
    except Exception as e:
        return {"error": str(e), "n_offline": 0}
    prices, sample = [], []
    for r in listings:
        lst = r.get("listing", {})
        if (lst.get("account") or {}).get("online"):
            continue  # OFFLINE-only
        a = boot_attrs(r.get("item", {}))
        if a.defc != target.defc:
            continue  # match defence type (post-fetch)
        p = lst.get("price") or {}
        d = _price_ex(p.get("amount"), p.get("currency", ""), rates, div) / div
        if d <= 0:
            continue
        prices.append(d)
        if len(sample) < 12:
            sample.append({"price_d": round(d, 2), "base": a.base, "ms": a.ms, "life": a.life,
                           "res": a.res, "rarity": a.rarity, "sockets": a.sockets})
    prices.sort()
    summ = None
    if prices:
        summ = {"floor_d": round(prices[0], 2),
                "p25_d": round(prices[len(prices) // 4], 2),
                "median_d": round(st.median(prices), 2),
                "p75_d": round(prices[min(len(prices) - 1, 3 * len(prices) // 4)], 2)}
    return {"total_listed_anystatus": total, "n_offline_matched": len(prices),
            "summary": summ, "sample": sample}


def appraise(cfg: Config, spec: dict) -> dict:
    from .stash import fetch_exalted_rates

    rates, div = fetch_exalted_rates(league=cfg.league)
    return {
        "league": cfg.league, "divine_price_ex": round(div),
        "item": {**spec, "mod_sig": _attrs_from_spec(spec).mod_sig},
        "comparable_sales": comparable_sales(cfg, spec, div, rates),
        "tracker": tracker_comps(cfg, spec, div),
        "live_offline": live_comparables(cfg, spec, div, rates),
    }


def main(argv: list[str] | None = None) -> None:
    """CLI: read item spec JSON from --json or stdin, print the evidence packet."""
    argv = argv if argv is not None else sys.argv[1:]
    if argv and argv[0] == "--json":
        spec = json.loads(argv[1])
    else:
        spec = json.loads(sys.stdin.read())
    cfg = Config.load()
    print(json.dumps(appraise(cfg, spec), indent=2))


if __name__ == "__main__":
    main()
