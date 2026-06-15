"""Divine-range boots analysis: sample boots by PRICE BAND and measure what mods
actually distinguish the 5-50 divine segment (vs cheap floors). Addresses the bias
that price-ascending floors only describe the bottom of each pool.

Run: PoE2_RUN_TAG=divrange_2026-06-14 python divrange_analysis.py
"""
import json
import os
import re
import statistics as st
import time

from poe2market.api import TradeAPI
from poe2market.config import Config
from poe2market.stash import fetch_exalted_rates

cfg = Config.load()
api = TradeAPI(cfg)
RATES, DIV = fetch_exalted_rates(league=cfg.league)
clean = lambda s: re.sub(r"\[([^|\]]+\|)?([^\]]+)\]", r"\2", s)

MS = "explicit.stat_2250533757"
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "research_data")
TAG = os.environ.get("PoE2_RUN_TAG", "divrange")
RAW = os.path.join(DATA_DIR, f"raw_divrange_{TAG}.jsonl")


def band_query(lo_div, hi_div):
    # IMPORTANT: price filter MUST use option "divine" for the divine range.
    # option "exalted" does NOT match divine-denominated listings, which makes
    # every band above ~2d look empty (the 2026-06-14 "thin market" error).
    f = [{"id": MS, "value": {"min": 30}, "disabled": False}]
    price = {"min": lo_div, "option": "divine"}
    if hi_div:
        price["max"] = hi_div
    return {
        "query": {
            "status": {"option": "online"},
            "stats": [{"type": "and", "filters": f}],
            "filters": {
                "type_filters": {"filters": {"category": {"option": "armour.boots"},
                                             "rarity": {"option": "rare"}, "ilvl": {"min": 79}}},
                "trade_filters": {"filters": {"price": price}},
            },
        },
        "sort": {"price": "asc"},
    }


def attrs(it):
    expl = [clean(m) for m in (it.get("explicitMods") or []) + (it.get("craftedMods") or [])
            + (it.get("desecratedMods") or [])]
    txt = " | ".join(expl)
    runes = " ".join(clean(m) for m in it.get("runeMods") or [])
    ms_e = max([int(m.group(1)) for x in expl if (m := re.match(r"(\d+)% increased Movement Speed", x))], default=0)
    ms_r = 5 if re.search(r"\d+% increased Movement Speed", runes) else 0
    life = max([int(m.group(1)) for x in expl if (m := re.match(r"\+(\d+) to maximum Life", x))], default=0)
    res = sum(int(x) for x in re.findall(r"\+(\d+)% to (?:Fire|Cold|Lightning) Resistance", txt))
    res += sum(int(x) for x in re.findall(r"\+(\d+)% to (?:Fire|Cold|Lightning) Resistance", runes))
    rarity = max([int(m.group(1)) for x in expl if (m := re.match(r"(\d+)% increased Rarity", x))], default=0)
    runic_ward = any("Runic Ward" in (p.get("name") or "") for p in it.get("properties") or [])
    runeforged = "Runeforged" in (it.get("typeLine") or "") or runic_ward
    DEF = {"Boots": "EV", "Sandals": "ES", "Slippers": "ES", "Shoes": "EV/ES",
           "Greaves": "AR", "Sabatons": "AR/EV", "Leggings": "AR/ES"}
    base = it.get("typeLine", "")
    dc = next((v for k, v in DEF.items() if k in base), "?")
    return dict(ms_e=ms_e, ms_pseudo=ms_e + ms_r, ms_rune=bool(ms_r), life=life, res=res,
                rarity=rarity, sockets=len(it.get("sockets") or []), runeforged=runeforged,
                runic_ward=runic_ward, corrupted=bool(it.get("corrupted")), base=base, defc=dc)


BANDS = [("5-10d", 5, 10), ("10-20d", 10, 20),
         ("20-50d", 20, 50), ("50d+", 50, None)]

if __name__ == "__main__":
    open(RAW, "w").close()
    rows_by_band = {}
    for label, lo, hi in BANDS:
        for attempt in range(4):
            try:
                qid, ids, total = api.search(band_query(lo, hi))
                break
            except Exception as e:
                print(f"  !! {label}: {e}; sleep 20", flush=True)
                time.sleep(20)
        else:
            continue
        got = []
        try:
            got = api.fetch(ids[:30], qid)
        except Exception as e:
            print(f"  !! fetch {label}: {e}", flush=True)
        recs = []
        with open(RAW, "a", encoding="utf-8") as fh:
            for r in got:
                a = attrs(r["item"])
                p = (r.get("listing") or {}).get("price") or {}
                a["price_ex"] = (p.get("amount") or 0) * RATES.get(p.get("currency"), {"divine": DIV, "exalted": 1}.get(p.get("currency"), 0))
                a["price_d"] = a["price_ex"] / DIV
                recs.append(a)
                fh.write(json.dumps({"_band": label, **a}) + "\n")
        rows_by_band[label] = (total, recs)
        print(f"band {label:<8} total_listed={total:<5} sampled={len(recs)}", flush=True)

    def pct(recs, pred):
        return f"{100*sum(1 for r in recs if pred(r))//max(1,len(recs)):>3d}%"

    print(f"\n{'band':<8} {'n':>3} {'life>=100':>9} {'life>=120':>9} {'rarity':>7} {'2-sock':>7} "
          f"{'MSrune':>7} {'MS>=40':>7} {'res>=70':>8} {'runefrg':>8} {'corrupt':>8}")
    for label, lo, hi in BANDS:
        if label not in rows_by_band:
            continue
        total, recs = rows_by_band[label]
        if not recs:
            print(f"{label:<8} 0")
            continue
        print(f"{label:<8} {len(recs):>3} "
              f"{pct(recs, lambda r: r['life']>=100):>9} {pct(recs, lambda r: r['life']>=120):>9} "
              f"{pct(recs, lambda r: r['rarity']>=10):>7} {pct(recs, lambda r: r['sockets']>=2):>7} "
              f"{pct(recs, lambda r: r['ms_rune']):>7} {pct(recs, lambda r: r['ms_pseudo']>=40):>7} "
              f"{pct(recs, lambda r: r['res']>=70):>8} {pct(recs, lambda r: r['runeforged']):>8} "
              f"{pct(recs, lambda r: r['corrupted']):>8}")

    # median life/res by band
    print(f"\n{'band':<8} {'med_life':>8} {'med_res':>8} {'med_MSpseudo':>12} {'defc_mix'}")
    for label, lo, hi in BANDS:
        if label not in rows_by_band:
            continue
        _, recs = rows_by_band[label]
        if not recs:
            continue
        from collections import Counter
        dcm = Counter(r["defc"] for r in recs).most_common(3)
        print(f"{label:<8} {st.median(r['life'] for r in recs):>8.0f} {st.median(r['res'] for r in recs):>8.0f} "
              f"{st.median(r['ms_pseudo'] for r in recs):>12.0f}   {dcm}")
