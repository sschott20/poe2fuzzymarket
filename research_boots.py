"""Ad-hoc market research harness for boots crafting analysis (Runes of Aldur)."""
import json
import os
import statistics as st
import sys
import time

from poe2market.api import TradeAPI
from poe2market.config import Config
from poe2market.stash import fetch_exalted_rates

cfg = Config.load()
api = TradeAPI(cfg)
RATES, DIV = fetch_exalted_rates(league=cfg.league)

# stat ids
MS = "explicit.stat_2250533757"
LIFE = "explicit.stat_3299347043"
RARITY = "explicit.stat_3917489142"
FIRE = "explicit.stat_3372524247"
COLD = "explicit.stat_4220027924"
LIGHT = "explicit.stat_1671376347"
P_LIFE = "pseudo.pseudo_total_life"
P_RES_T = "pseudo.pseudo_total_elemental_resistance"  # sum of all ele res
R_IRON = "rune.stat_3523867985"     # % increased Armour, Evasion and Energy Shield
R_COLD = "rune.stat_4220027924"
R_FIRE = "rune.stat_3372524247"
R_LIGHT = "rune.stat_1671376347"
R_MS = "rune.stat_2250533757"
PSEU_MS = "pseudo.pseudo_increased_movement_speed"


def q(stat_filters=None, base=None, rune_sockets=None, corrupted=None,
      rarity="rare", ilvl_min=79, online=True, quality_min=None):
    f = []
    for sid, mn, mx in (stat_filters or []):
        ff = {"id": sid, "disabled": False}
        v = {}
        if mn is not None:
            v["min"] = mn
        if mx is not None:
            v["max"] = mx
        if v:
            ff["value"] = v
        f.append(ff)
    filters = {
        "type_filters": {"filters": {"category": {"option": "armour.boots"},
                                     "rarity": {"option": rarity},
                                     "ilvl": {"min": ilvl_min}}},
    }
    if quality_min is not None:
        filters["type_filters"]["filters"]["quality"] = {"min": quality_min}
    if base:
        pass  # via query.type below
    if rune_sockets is not None:
        filters["equipment_filters"] = {"filters": {"rune_sockets": rune_sockets}}
    if corrupted is not None:
        filters.setdefault("misc_filters", {"filters": {}})["filters"]["corrupted"] = {
            "option": "true" if corrupted else "false"}
    query = {
        "query": {
            "status": {"option": "online" if online else "any"},
            "stats": [{"type": "and", "filters": f}],
            "filters": filters,
        },
        "sort": {"price": "asc"},
    }
    if base:
        query["query"]["type"] = base
    return query


def to_ex(amount, currency):
    r = RATES.get(currency)
    if r is None:
        alias = {"exalted": 1.0, "divine": DIV, "chaos": RATES.get("chaos", 1 / 12)}
        r = alias.get(currency, 0)
    return amount * r


# Durable persistence: every run() appends here so search data is never lost to
# stdout-only output (which is how the 2026-06 session nearly lost everything).
# RUN_DIR is stamped via env (PoE2_RUN_TAG) or defaults to the block name; the
# caller sets the date so this file stays import-without-Date-clean.
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "research_data")
os.makedirs(DATA_DIR, exist_ok=True)
RUN_TAG = os.environ.get("PoE2_RUN_TAG", "adhoc")
RAW_PATH = os.path.join(DATA_DIR, f"raw_listings_{RUN_TAG}.jsonl")
RESULTS_PATH = os.path.join(DATA_DIR, f"results_{RUN_TAG}.jsonl")


def _save_raw(label, listings):
    with open(RAW_PATH, "a", encoding="utf-8") as fh:
        for r in listings:
            fh.write(json.dumps({"_segment": label, "_run": RUN_TAG, **r}) + "\n")


def _save_result(record):
    with open(RESULTS_PATH, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")


def run(label, query, n_fetch=10, retries=3):
    for attempt in range(retries):
        try:
            qid, ids, total = api.search(query)
            break
        except Exception as e:
            print(f"  !! {label}: {e}; retrying in 20s", flush=True)
            time.sleep(20)
    else:
        print(f"  !! {label}: FAILED")
        _save_result({"label": label, "status": "FAILED", "query": query})
        return None
    prices = []
    listings = []
    if ids:
        try:
            listings = api.fetch(ids[:n_fetch], qid)
            for r in listings:
                listing = r.get("listing", {})
                price = listing.get("price") or {}
                amt, ccy = price.get("amount"), price.get("currency")
                if amt:
                    prices.append(to_ex(amt, ccy))
        except Exception as e:
            print(f"  !! fetch {label}: {e}")
    prices.sort()
    if listings:
        _save_raw(label, listings)  # full item JSON, re-analyzable later
    if prices:
        med10 = st.median(prices)
        print(f"{label:<58} n={total:<5} floor={prices[0]:7.1f}ex  "
              f"5th={prices[4] if len(prices)>4 else prices[-1]:7.1f}ex  med10={med10:7.1f}ex  "
              f"({prices[0]/DIV:.2f}d / {med10/DIV:.2f}d)", flush=True)
    else:
        print(f"{label:<58} n={total:<5} (no priced results)", flush=True)
    record = {"label": label, "total": total, "prices": prices,
              "floor_ex": prices[0] if prices else None,
              "median_ex": st.median(prices) if prices else None, "div": DIV}
    _save_result(record)
    return record


if __name__ == "__main__":
    out = []
    block = sys.argv[1] if len(sys.argv) > 1 else "all"
    print(f"divine = {DIV:.0f}ex | block: {block}")

    if block in ("q1", "all"):
        print("\n=== Q1: rune type, matched profile MS>=35 + life>=100, uncorrupted ===")
        base_stats = [(MS, 35, None), (LIFE, 100, None)]
        run("1 socket | glacial rune (cold res 15-20)", q(base_stats + [(R_COLD, 15, 20)], corrupted=False))
        run("1 socket | fire rune 15-20", q(base_stats + [(R_FIRE, 15, 20)], corrupted=False))
        run("1 socket | lightning rune 15-20", q(base_stats + [(R_LIGHT, 15, 20)], corrupted=False))
        run("1 socket | iron rune (incArEvES 15-20)", q(base_stats + [(R_IRON, 15, 20)], corrupted=False))
        run("2 sockets | 2x glacial (cold res >=30)", q(base_stats + [(R_COLD, 30, None)], corrupted=False))
        run("2 sockets | 2x iron (incArEvES >=30)", q(base_stats + [(R_IRON, 30, None)], corrupted=False))
        run("2 sockets | MS rune + any (rune MS>=5)", q(base_stats + [(R_MS, 5, None)], corrupted=False))
        print("\n--- same but no life requirement (bigger sample) ---")
        run("MS35 | glacial 15-20", q([(MS, 35, None), (R_COLD, 15, 20)], corrupted=False))
        run("MS35 | iron 15-20", q([(MS, 35, None), (R_IRON, 15, 20)], corrupted=False))
        run("MS35 | glacial >=30 (2 runes)", q([(MS, 35, None), (R_COLD, 30, None)], corrupted=False))
        run("MS35 | iron >=30 (2 runes)", q([(MS, 35, None), (R_IRON, 30, None)], corrupted=False))
        run("MS35 | MS rune >=5", q([(MS, 35, None), (R_MS, 5, None)], corrupted=False))

    if block in ("life", "all"):
        print("\n=== Q5a: life premium, whole category, MS35 + total res>=60, uncorrupted ===")
        base = [(MS, 35, None), (P_RES_T, 60, None)]
        run("MS35 res60 | life any (baseline)", q(base, corrupted=False))
        run("MS35 res60 | life>=70", q(base + [(LIFE, 70, None)], corrupted=False))
        run("MS35 res60 | life>=100", q(base + [(LIFE, 100, None)], corrupted=False))
        run("MS35 res60 | life>=120", q(base + [(LIFE, 120, None)], corrupted=False))
        run("MS35 res60 | life>=140", q(base + [(LIFE, 140, None)], corrupted=False))

        print("\n=== Q5b: per-base life premium (life>=100 vs life-any), MS35 uncorrupted ===")
        for b in ["Sandsworn Sandals", "Sekhema Sandals", "Cinched Boots",
                  "Dragonscale Boots", "Quickslip Shoes", "Daggerfoot Shoes"]:
            run(f"{b} | MS35 life any", q([(MS, 35, None)], base=b, corrupted=False))
            run(f"{b} | MS35 life>=100", q([(MS, 35, None), (LIFE, 100, None)], base=b, corrupted=False))

        print("\n=== Q5c: life-essence vs rarity-essence finished product, MS35 res60 uncorrupted ===")
        run("LIFE build: MS35 res60 life>=120 (no rarity)", q([(MS, 35, None), (P_RES_T, 60, None), (LIFE, 120, None)], corrupted=False))
        run("RARITY build: MS35 res60 rarity>=15 (life any)", q([(MS, 35, None), (P_RES_T, 60, None), (RARITY, 15, None)], corrupted=False))
        run("BOTH: MS35 res60 rarity>=15 life>=120", q([(MS, 35, None), (P_RES_T, 60, None), (RARITY, 15, None), (LIFE, 120, None)], corrupted=False))
        run("RARITY no life: MS35 res60 rarity>=15 life<=1", q([(MS, 35, None), (P_RES_T, 60, None), (RARITY, 15, None), (LIFE, None, 1)], corrupted=False))

    if block in ("msrune", "all"):
        print("\n=== Q6a: above-cap MS premium, life>=100 res>=40, uncorrupted ===")
        b = [(LIFE, 100, None), (P_RES_T, 40, None)]
        run("native 35 only (explicit MS=35)", q([(MS, 35, 35)] + b, corrupted=False))
        run("above-cap: pseudo MS>=40 (35+rune)", q([(PSEU_MS, 40, None)] + b, corrupted=False))
        run("above-cap: pseudo MS>=38", q([(PSEU_MS, 38, None)] + b, corrupted=False))

        print("\n=== Q6b: rescue a 30% craft — does 30+rune match/sell like 35? life>=100 res>=40 ===")
        run("native 35 (explicit=35)", q([(MS, 35, 35)] + b, corrupted=False))
        run("30%+rune (expl=30, pseudo>=35)", q([(MS, 30, 30), (R_MS, 5, None)] + b, corrupted=False))
        run("explicit 30 NO rune (pseudo<35)", q([(MS, 30, 30), (PSEU_MS, None, 34)] + b, corrupted=False))
        run("buyers using pseudo>=35 (any source)", q([(PSEU_MS, 35, None)] + b, corrupted=False))

        print("\n=== Q6c: 1-socket opportunity cost — MS rune vs resist rune, MS35 life>=100 ===")
        run("MS35 life100 + MS rune", q([(MS, 35, None), (LIFE, 100, None), (R_MS, 5, None)], corrupted=False))
        run("MS35 life100 + glacial rune", q([(MS, 35, None), (LIFE, 100, None), (R_COLD, 15, None)], corrupted=False))
        run("MS35 life100 baseline", q([(MS, 35, None), (LIFE, 100, None)], corrupted=False))

        print("\n=== Q6d: 2-socket combos (MS rune + resist rune), uncorrupted ===")
        run("2sock: MS rune + total res>=70 + life100", q([(R_MS, 5, None), (P_RES_T, 70, None), (LIFE, 100, None)], rune_sockets={"min": 2}, corrupted=False))
        run("pseudo MS>=40 + res>=70 + life>=100 (god)", q([(PSEU_MS, 40, None), (P_RES_T, 70, None), (LIFE, 100, None)], corrupted=False))

    if block == "sweep":
        print("\n=== price-band sweep: rare boots MS>=30 ilvl>=79, online ===")
        bands = [(1, 9), (10, 29), (30, 59), (60, 132), (133, 265), (266, 531),
                 (532, 1064), (1065, 1999), (2000, 3999), (4000, None)]
        rows = []
        for lo, hi in bands:
            query = q([(MS, 30, None)])
            query["query"]["filters"]["trade_filters"] = {
                "filters": {"price": {"min": lo, **({"max": hi} if hi else {}), "option": "exalted"}}}
            for attempt in range(4):
                try:
                    qid, ids, total = api.search(query)
                    break
                except Exception as e:
                    print(f"  !! band {lo}-{hi}: {e}; sleep 20s", flush=True)
                    time.sleep(20)
            else:
                continue
            got = []
            try:
                got = api.fetch(ids[:20], qid)
            except Exception as e:
                print(f"  !! fetch band {lo}-{hi}: {e}", flush=True)
            print(f"band {lo}-{hi}ex: total={total} fetched={len(got)}", flush=True)
            for r in got:
                rows.append(r)
        with open("boots_sweep.jsonl", "w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r) + "\n")
        print(f"saved {len(rows)} listings to boots_sweep.jsonl")

    if block == "comps":
        import datetime
        segs = [
            ("res70+", q([(MS, 35, None), (LIFE, 100, None), (P_RES_T, 70, None)], corrupted=False)),
            ("rarity15+res35", q([(MS, 35, None), (LIFE, 100, None), (RARITY, 15, None), (P_RES_T, 35, None)], corrupted=False)),
            ("corrupted res60", q([(MS, 35, None), (LIFE, 100, None), (P_RES_T, 60, None)], corrupted=True)),
            ("2sock res50", q([(MS, 35, None), (LIFE, 100, None), (P_RES_T, 50, None)], rune_sockets={"min": 2}, corrupted=False)),
        ]
        now = datetime.datetime.now(datetime.timezone.utc)
        for name, query in segs:
            try:
                qid, ids, total = api.search(query)
            except Exception as e:
                print(f"!! {name}: {e}"); time.sleep(20); continue
            print(f"\n### segment {name}: n={total}")
            try:
                got = api.fetch(ids[:20], qid)
            except Exception as e:
                print(f"!! fetch: {e}"); continue
            for r in got:
                it, lst = r["item"], r["listing"]
                p = lst.get("price") or {}
                ex = to_ex(p.get("amount", 0), p.get("currency", ""))
                idx = lst.get("indexed", "")
                age_d = ""
                if idx:
                    dt = datetime.datetime.fromisoformat(idx.replace("Z", "+00:00"))
                    age_d = f"{(now - dt).total_seconds()/86400:.1f}d-old"
                mods = [m for m in (it.get("explicitMods") or []) + (it.get("craftedMods") or [])]
                runes = [m for m in it.get("runeMods") or [] if not m.startswith("Bonded")]
                print(f"  {ex:7.0f}ex ({ex/DIV:5.2f}d) {age_d:>10} | {it.get('typeLine'):<22} sock={len(it.get('sockets') or [])} corr={it.get('corrupted', False)}")
                print(f"      runes: {'; '.join(runes)}")
                print(f"      {' | '.join(mods)}")

    if block in ("q2", "all"):
        print("\n=== Q2: essence mod — rarity vs resist, matched MS>=35 life>=100 ===")
        base_stats = [(MS, 35, None), (LIFE, 100, None)]
        run("+ rarity 10-20 (essence tier)", q(base_stats + [(RARITY, 10, 20)], corrupted=False))
        run("+ rarity >=20", q(base_stats + [(RARITY, 20, None)], corrupted=False))
        run("+ single fire res >=30", q(base_stats + [(FIRE, 30, None)], corrupted=False))
        run("+ single cold res >=30", q(base_stats + [(COLD, 30, None)], corrupted=False))
        run("+ single light res >=30", q(base_stats + [(LIGHT, 30, None)], corrupted=False))
        run("+ total ele res >=70 (pseudo)", q(base_stats + [(P_RES_T, 70, None)], corrupted=False))
        run("MS35 life>=100 only (baseline)", q(base_stats, corrupted=False))
        run("rarity>=15 AND total res>=35", q(base_stats + [(RARITY, 15, None), (P_RES_T, 35, None)], corrupted=False))

    if block in ("q3", "all"):
        print("\n=== Q3a: base type, MS>=30 life>=80, uncorrupted rare ===")
        for b in ["Cinched Boots", "Dragonscale Boots", "Daggerfoot Shoes", "Quickslip Shoes",
                  "Charmed Shoes", "Sandsworn Sandals", "Sekhema Sandals", "Cavalry Boots",
                  "Wanderer Shoes", "Drakeskin Boots", "Serpentscale Boots", "Stormrider Boots"]:
            run(f"base: {b}", q([(MS, 30, None), (LIFE, 80, None)], base=b, corrupted=False))
        print("\n=== Q3b: socket-count premium, MS>=35 life>=100 ===")
        bs = [(MS, 35, None), (LIFE, 100, None)]
        run("finished rare, 1 socket max", q(bs, rune_sockets={"min": 1, "max": 1}, corrupted=False))
        run("finished rare, 2 sockets", q(bs, rune_sockets={"min": 2}, corrupted=False))
        print("\n=== Q3c: craft input cost — magic MS35 base, 1 vs 2 sockets ===")
        run("magic MS>=35 input, sockets<=1", q([(MS, 35, None)], rarity="magic", rune_sockets={"max": 1}))
        run("magic MS>=35 input, 2 sockets", q([(MS, 35, None)], rarity="magic", rune_sockets={"min": 2}))
        run("magic MS>=35 +res input, <=1 sock", q([(MS, 35, None), (P_RES_T, 25, None)], rarity="magic", rune_sockets={"max": 1}))
        run("magic MS>=35 +res input, 2 sock", q([(MS, 35, None), (P_RES_T, 25, None)], rarity="magic", rune_sockets={"min": 2}))

    if block in ("q4", "all"):
        print("\n=== Q4: corruption premium/discount, matched profiles ===")
        bs = [(MS, 35, None), (LIFE, 100, None)]
        run("MS35 life100, NOT corrupted", q(bs, corrupted=False))
        run("MS35 life100, corrupted", q(bs, corrupted=True))
        bs2 = [(MS, 35, None), (LIFE, 100, None), (P_RES_T, 60, None)]
        run("MS35 life100 res60+, NOT corrupted", q(bs2, corrupted=False))
        run("MS35 life100 res60+, corrupted", q(bs2, corrupted=True))
