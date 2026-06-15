"""Boots sale-tracker: infer real *clearing* prices from listing churn.

Live trade listings only show asking prices. For low-volume chase boots that is
nearly useless — a listing sitting at 40d tells you nothing about what it will
actually sell for. This module polls the boots market on a schedule, tracks each
listing's lifecycle across polls, and treats two events as signal:

  * a reprice DOWN  -> the previous price was too high (a soft ceiling)
  * a disappearance -> the listing was removed (sold, or delisted)

By aggregating *exit prices* (price at last sighting before a listing vanished)
and time-on-market per mod-bucket, you get a far better estimate of where items
actually clear than any asking-price snapshot.

OFFLINE-ONLY by default (user preference): the trade API has no "offline" status,
so we search ``status: "any"`` and keep only listings whose seller is offline.
This is also methodologically better here — an offline listing does not drop out
of results when the seller logs off, so its disappearance is a cleaner "removed"
(i.e. likely sold) signal than an online listing, which vanishes the moment the
seller closes the game.

Focus (configurable): the bases this crafter sells — dex (Evasion) and dex/int
(Evasion+Energy-Shield) boots — with extra weight on 2-socket items, which are
the most volatile / highest-value segment. All boots are stored and tagged by
defence type; the report filters to the focus set.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

from .observations import defence_flags

# ---- stat ids (boots) -------------------------------------------------------
MS = "explicit.stat_2250533757"
LIFE = "explicit.stat_3299347043"
RARITY = "explicit.stat_3917489142"
P_RES = "pseudo.pseudo_total_elemental_resistance"     # sum of all ele res (incl runes)
P_CHAOS = "pseudo.pseudo_total_chaos_resistance"       # total chaos res (incl runes)
PSEU_MS = "pseudo.pseudo_increased_movement_speed"     # explicit + rune MS

_MARKUP = re.compile(r"\[([^|\]]+\|)?([^\]]+)\]")
_clean = lambda s: _MARKUP.sub(r"\2", s or "")

# A listing must miss this many consecutive polls before we call it "gone".
GONE_AFTER = 2
# Defence types we craft/sell: pure-int (ES), pure-dex (EV), dex-int (EV+ES).
# Anything with Armour is ignored entirely (not tracked, not reported).
FOCUS_DEFENCE = ("ES", "EV", "EV+ES")
# Ignore the mirror-fishing bracket — the user rarely crafts/sells above this, so
# tracking it just adds noise and skews estimates high.
MAX_TRACK_DIV = 400.0


# ---- mod bucketing ----------------------------------------------------------

def _ms_tier(ms_pseudo: int) -> str:
    if ms_pseudo >= 36:
        return "36+"        # above the 35 cap (rune-boosted) — the chase tier
    if ms_pseudo >= 35:
        return "35"
    if ms_pseudo >= 30:
        return "30-34"
    return "<30"


def _res_bucket(res: int) -> str:
    if res >= 100:
        return "100+"
    if res >= 70:
        return "70-99"
    if res >= 40:
        return "40-69"
    return "0-39"


def _life_bucket(life: int) -> str:
    if life >= 120:
        return "120+"
    if life >= 80:
        return "80-119"
    if life >= 1:
        return "1-79"
    return "0"


def defence_type(item: dict) -> str:
    f = defence_flags(item)
    tags = [t for t, k in (("AR", "ar"), ("EV", "ev"), ("ES", "es")) if f[k]]
    return "+".join(tags) if tags else "?"


def _chaos_tag(chaos: int) -> str:
    if chaos >= 15:
        return "|ch15+"
    if chaos >= 8:
        return "|ch8+"
    return ""


@dataclass
class BootAttrs:
    base: str
    defc: str
    sockets: int
    ms: int           # pseudo MS (explicit + rune) — what buyers search
    life: int
    res: int          # total elemental res (explicit + rune), matches trade pseudo
    rarity: int
    corrupted: bool
    runeforged: bool
    ms_explicit: int = 0    # explicit MS only (rune-boosted 35 vs native 35 differ in value)
    ms_rune: bool = False   # is part of the MS coming from a rune?
    chaos_res: int = 0      # +#% to Chaos Resistance
    mod_sig: str = ""

    def signature(self) -> str:
        # "r" on the MS tier marks rune-boosted MS (e.g. 30 explicit + 5 rune = 35r),
        # so a native 35 and a rune-boosted 35 land in separate, correctly-priced buckets.
        sig = (f"{self.defc}|{self.sockets}s"
               f"|ms{_ms_tier(self.ms)}{'r' if self.ms_rune else ''}"
               f"|res{_res_bucket(self.res)}|life{_life_bucket(self.life)}"
               f"|rar{'Y' if self.rarity >= 10 else 'N'}")
        sig += _chaos_tag(self.chaos_res)
        if self.corrupted:
            sig += "|corr"
        if self.runeforged:
            sig += "|rf"
        return sig


def boot_attrs(item: dict) -> BootAttrs:
    expl = [_clean(m) for m in (item.get("explicitMods") or [])
            + (item.get("craftedMods") or []) + (item.get("desecratedMods") or [])]
    runes = " ".join(_clean(m) for m in item.get("runeMods") or [])
    txt = " | ".join(expl)
    ms_e = max([int(m.group(1)) for x in expl
                if (m := re.match(r"(\d+)% increased Movement Speed", x))], default=0)
    ms_rune = 5 if re.search(r"\d+% increased Movement Speed", runes) else 0
    life = max([int(m.group(1)) for x in expl
                if (m := re.match(r"\+(\d+) to maximum Life", x))], default=0)
    res = (sum(int(x) for x in re.findall(r"\+(\d+)% to (?:Fire|Cold|Lightning) Resistance", txt))
           + sum(int(x) for x in re.findall(r"\+(\d+)% to (?:Fire|Cold|Lightning) Resistance", runes)))
    chaos = (sum(int(x) for x in re.findall(r"\+(\d+)% to Chaos Resistance", txt))
             + sum(int(x) for x in re.findall(r"\+(\d+)% to Chaos Resistance", runes)))
    rarity = max([int(m.group(1)) for x in expl
                  if (m := re.match(r"(\d+)% increased Rarity", x))], default=0)
    base = _clean(item.get("typeLine", ""))
    runeforged = "Runeforged" in base or any(
        "Runic Ward" in _clean(p.get("name", "")) for p in item.get("properties") or [])
    a = BootAttrs(base=base, defc=defence_type(item), sockets=len(item.get("sockets") or []),
                  ms=ms_e + ms_rune, life=life, res=res, rarity=rarity,
                  corrupted=bool(item.get("corrupted")), runeforged=runeforged,
                  ms_explicit=ms_e, ms_rune=bool(ms_rune), chaos_res=chaos)
    a.mod_sig = a.signature()
    return a


# ---- search profiles --------------------------------------------------------

@dataclass
class Profile:
    """A tight, fully-enumerable product bucket.

    The key requirement for lifecycle tracking is a STABLE cohort: the search
    must match few enough items that we fetch them ALL every poll. Then a
    disappearance is a real removal (sold/delisted), not just "fell out of a
    cheapest-N sample of a huge band". Each profile here is the kind of boot
    this crafter actually sells, filtered tight enough to enumerate.
    """
    name: str
    rune_sockets: dict | None              # {"min": 2} or {"max": 1}
    stats: list[tuple[str, float | None, float | None]]   # (stat_id, min, max)
    min_div: float = 12.0                  # focus the chase tier; reprices stay in scope
    max_div: float = MAX_TRACK_DIV         # ignore the mirror-fishing bracket above this
    max_pages: int = 3                     # price-walk pages (each <=100); caps API load

    def query(self, floor_div: float | None = None) -> dict:
        # price option MUST be "divine" (exalted filter misses divine listings).
        # floor_div lets the poller "scroll" past 100 by raising the price floor.
        lo = self.min_div if floor_div is None else floor_div
        price = {"min": lo, "max": MAX_TRACK_DIV, "option": "divine"}
        sfilters = []
        for sid, mn, mx in self.stats:
            v = {}
            if mn is not None:
                v["min"] = mn
            if mx is not None:
                v["max"] = mx
            sfilters.append({"id": sid, "value": v, "disabled": False})
        filters = {
            "type_filters": {"filters": {
                "category": {"option": "armour.boots"},
                "rarity": {"option": "rare"},
                "ilvl": {"min": 79},
            }},
            "trade_filters": {"filters": {"price": price}},
        }
        if self.rune_sockets is not None:
            filters["equipment_filters"] = {"filters": {"rune_sockets": self.rune_sockets}}
        return {
            "query": {
                "status": {"option": "any"},   # offline filtering happens in code
                "stats": [{"type": "and", "filters": sfilters}],
                "filters": filters,
            },
            "sort": {"price": "asc"},
        }


# Product buckets matching what this crafter sells. Each is tight enough to
# fully enumerate (verify total stays well under ~100). 2-socket = the volatile
# high-value segment, weighted with its own dedicated buckets. Defence type is
# NOT filtered server-side (no clean API option) — it's tagged post-fetch and
# the report filters to EV / EV+ES.
DEFAULT_PROFILES = [
    # 2-socket (volatile chase) — tight combos, price-walked to full enumeration
    Profile("2s-ms35-res100",        {"min": 2}, [(MS, 35, None), (P_RES, 100, None)], min_div=15),
    Profile("2s-ms35-res70-life100", {"min": 2}, [(MS, 35, None), (P_RES, 70, None), (LIFE, 100, None)], min_div=12),
    Profile("2s-abovecap-res70",     {"min": 2}, [(PSEU_MS, 40, None), (P_RES, 70, None)], min_div=15),
    # 1-socket bread-and-butter — res70+rarity (res100 too) is the core product,
    # tracked from 8d since these sell in the 8-40d range
    Profile("1s-ms35-res70-rar15",   {"max": 1}, [(MS, 35, None), (P_RES, 70, None), (RARITY, 15, None)], min_div=8),
    Profile("2s-ms35-res70-rar15",   {"min": 2}, [(MS, 35, None), (P_RES, 70, None), (RARITY, 15, None)], min_div=8),
    Profile("1s-ms35-res70-life120", {"max": 1}, [(MS, 35, None), (P_RES, 70, None), (LIFE, 120, None)], min_div=15),
    # extra coverage for chaos-resistance boots (a valued, scarcer suffix).
    # Boots whose chaos pairs with high ele res are already tracked by the res
    # profiles (chaos is tagged in the mod_sig); these catch chaos-primary boots.
    Profile("2s-ms35-chaos",         {"min": 2}, [(MS, 35, None), (P_CHAOS, 10, None)], min_div=12),
    Profile("1s-ms35-chaos-res70",   {"max": 1}, [(MS, 35, None), (P_CHAOS, 10, None), (P_RES, 70, None)], min_div=12),
]


# ---- store ------------------------------------------------------------------

@dataclass
class PollResult:
    league: str
    poll_ts: float
    seen: int = 0
    offline: int = 0
    new: int = 0
    repriced_up: int = 0
    repriced_down: int = 0
    marked_gone: int = 0
    by_profile: dict = field(default_factory=dict)


class TrackerStore:
    def __init__(self, cache_dir: str):
        self.db_path = Path(cache_dir) / "tracker.db"
        Path(cache_dir).mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as c:
            c.execute("""
                CREATE TABLE IF NOT EXISTS sightings (
                    league TEXT, item_hash TEXT, poll_ts REAL, mod_sig TEXT,
                    base TEXT, defc TEXT, sockets INT, ms INT, life INT, res INT,
                    rarity INT, corrupted INT, runeforged INT,
                    amount REAL, currency TEXT, price_ex REAL,
                    account TEXT, online INT, indexed TEXT,
                    ms_explicit INT, chaos_res INT,
                    PRIMARY KEY (league, item_hash, poll_ts)
                )""")
            c.execute("""
                CREATE TABLE IF NOT EXISTS listings (
                    league TEXT, item_hash TEXT, base TEXT, defc TEXT, sockets INT,
                    mod_sig TEXT, ms INT, life INT, res INT, rarity INT,
                    corrupted INT, runeforged INT,
                    first_seen REAL, last_seen REAL,
                    first_price_ex REAL, last_price_ex REAL,
                    min_price_ex REAL, max_price_ex REAL,
                    n_sightings INT, n_reprice_up INT, n_reprice_down INT,
                    last_online INT, status TEXT, missed_polls INT,
                    exit_price_ex REAL, exit_ts REAL,
                    ms_explicit INT, chaos_res INT,
                    PRIMARY KEY (league, item_hash)
                )""")
            # migrate older DBs that predate the ms_explicit / chaos_res columns
            for tbl in ("sightings", "listings"):
                for col in ("ms_explicit", "chaos_res"):
                    try:
                        c.execute(f"ALTER TABLE {tbl} ADD COLUMN {col} INT")
                    except sqlite3.OperationalError:
                        pass  # already exists
            c.execute("CREATE INDEX IF NOT EXISTS idx_listings_sig ON listings (league, mod_sig)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_listings_status ON listings (league, status)")

    # -- write one poll's offline listings --
    def record_offline(self, league: str, rows: list[tuple[str, BootAttrs, float, float, str, str]],
                       poll_ts: float, result: PollResult) -> None:
        """rows: (item_hash, attrs, price_ex, amount, currency, indexed)."""
        with sqlite3.connect(self.db_path) as c:
            for item_hash, a, price_ex, amount, currency, indexed in rows:
                c.execute("""INSERT OR REPLACE INTO sightings VALUES
                    (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (league, item_hash, poll_ts, a.mod_sig, a.base, a.defc, a.sockets,
                     a.ms, a.life, a.res, a.rarity, int(a.corrupted), int(a.runeforged),
                     amount, currency, price_ex, "", 0, indexed, a.ms_explicit, a.chaos_res))
                prev = c.execute(
                    "SELECT last_price_ex, n_sightings, n_reprice_up, n_reprice_down, "
                    "min_price_ex, max_price_ex, first_seen, first_price_ex "
                    "FROM listings WHERE league=? AND item_hash=?",
                    (league, item_hash)).fetchone()
                if prev is None:
                    c.execute("""INSERT INTO listings VALUES
                        (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (league, item_hash, a.base, a.defc, a.sockets, a.mod_sig,
                         a.ms, a.life, a.res, a.rarity, int(a.corrupted), int(a.runeforged),
                         poll_ts, poll_ts, price_ex, price_ex, price_ex, price_ex,
                         1, 0, 0, 0, "active", 0, None, None, a.ms_explicit, a.chaos_res))
                    result.new += 1
                else:
                    (last_p, n, up, down, mn, mx, first_seen, first_p) = prev
                    if price_ex > last_p + 1e-9:
                        up += 1; result.repriced_up += 1
                    elif price_ex < last_p - 1e-9:
                        down += 1; result.repriced_down += 1
                    c.execute("""UPDATE listings SET last_seen=?, last_price_ex=?,
                        min_price_ex=?, max_price_ex=?, n_sightings=?, n_reprice_up=?,
                        n_reprice_down=?, status='active', missed_polls=0,
                        exit_price_ex=NULL, exit_ts=NULL, mod_sig=?, ms=?, life=?, res=?,
                        rarity=?, corrupted=?, runeforged=?, ms_explicit=?, chaos_res=?
                        WHERE league=? AND item_hash=?""",
                        (poll_ts, price_ex, min(mn, price_ex), max(mx, price_ex), n + 1,
                         up, down, a.mod_sig, a.ms, a.life, a.res, a.rarity,
                         int(a.corrupted), int(a.runeforged), a.ms_explicit, a.chaos_res,
                         league, item_hash))

    def mark_absent(self, league: str, seen_hashes: set[str], poll_ts: float,
                    result: PollResult) -> None:
        """Active listings not seen this poll: bump missed_polls; mark gone past
        the threshold, recording the last seen price as the exit price."""
        with sqlite3.connect(self.db_path) as c:
            active = c.execute(
                "SELECT item_hash, missed_polls, last_price_ex, last_seen "
                "FROM listings WHERE league=? AND status='active'", (league,)).fetchall()
            for item_hash, missed, last_p, last_seen in active:
                if item_hash in seen_hashes:
                    continue
                missed += 1
                if missed >= GONE_AFTER:
                    c.execute("UPDATE listings SET status='gone', missed_polls=?, "
                              "exit_price_ex=?, exit_ts=? WHERE league=? AND item_hash=?",
                              (missed, last_p, last_seen, league, item_hash))
                    result.marked_gone += 1
                else:
                    c.execute("UPDATE listings SET missed_polls=? WHERE league=? AND item_hash=?",
                              (missed, league, item_hash))

    # -- analysis --
    def clearing_report(self, league: str, defence: tuple[str, ...] = FOCUS_DEFENCE,
                        min_exits: int = 2) -> list[dict]:
        """Per mod-bucket clearing stats from 'gone' listings (exit prices)."""
        import statistics as st
        with sqlite3.connect(self.db_path) as c:
            rows = c.execute(
                "SELECT mod_sig, defc, sockets, exit_price_ex, last_seen, first_seen, "
                "n_reprice_down, n_reprice_up FROM listings "
                "WHERE league=? AND status='gone' AND exit_price_ex IS NOT NULL",
                (league,)).fetchall()
        groups: dict[str, list] = {}
        for sig, defc, sockets, exit_p, last_seen, first_seen, down, up in rows:
            if defence and defc not in defence:
                continue
            groups.setdefault(sig, []).append(
                (exit_p, (last_seen - first_seen) / 86400.0, down, up))
        out = []
        for sig, vals in groups.items():
            if len(vals) < min_exits:
                continue
            exits = sorted(v[0] for v in vals)
            out.append({
                "mod_sig": sig, "n_exits": len(vals),
                "median_exit_ex": st.median(exits),
                "p25_exit_ex": exits[len(exits) // 4],
                "p75_exit_ex": exits[min(len(exits) - 1, 3 * len(exits) // 4)],
                "median_days_on_market": round(st.median(v[1] for v in vals), 2),
                "reprice_down_rate": round(sum(1 for v in vals if v[2] > 0) / len(vals), 2),
            })
        out.sort(key=lambda r: -r["median_exit_ex"])
        return out

    def stale_ceilings(self, league: str, defence: tuple[str, ...] = FOCUS_DEFENCE,
                       min_age_days: float = 0.5) -> list[dict]:
        """Active listings sitting unsold = soft price ceilings, tagged by how long
        they've sat (12h+ / 1d+ / 3d+). Default floor 12h."""
        now = time.time()
        with sqlite3.connect(self.db_path) as c:
            rows = c.execute(
                "SELECT mod_sig, defc, last_price_ex, first_seen, n_reprice_down "
                "FROM listings WHERE league=? AND status='active'", (league,)).fetchall()
        out = []
        for sig, defc, price, first_seen, down in rows:
            if defence and defc not in defence:
                continue
            age = (now - first_seen) / 86400.0
            if age >= min_age_days:
                tier = "3d+" if age >= 3 else "1d+" if age >= 1 else "12h+"
                out.append({"mod_sig": sig, "price_ex": price, "age_days": round(age, 2),
                            "tier": tier, "reprice_downs": down})
        out.sort(key=lambda r: -r["age_days"])
        return out

    def exit_medians(self, league: str, defence: tuple[str, ...] | None = None) -> dict[str, float]:
        """median exit price (ex) per mod_sig, from 'gone' listings."""
        import statistics as st
        with sqlite3.connect(self.db_path) as c:
            rows = c.execute(
                "SELECT mod_sig, defc, exit_price_ex FROM listings "
                "WHERE league=? AND status='gone' AND exit_price_ex IS NOT NULL",
                (league,)).fetchall()
        g: dict[str, list] = {}
        for sig, defc, px in rows:
            if defence and defc not in defence:
                continue
            g.setdefault(sig, []).append(px)
        return {sig: st.median(v) for sig, v in g.items()}

    def all_listings(self, league: str) -> list[dict]:
        """Every tracked item with its current lifecycle state, priciest first."""
        with sqlite3.connect(self.db_path) as c:
            rows = c.execute(
                "SELECT base, defc, sockets, ms, life, res, rarity, corrupted, runeforged, "
                "last_price_ex, min_price_ex, max_price_ex, n_sightings, n_reprice_up, "
                "n_reprice_down, status, first_seen, last_seen, exit_price_ex, "
                "COALESCE(ms_explicit, ms), COALESCE(chaos_res, 0) "
                "FROM listings WHERE league=? ORDER BY last_price_ex DESC", (league,)).fetchall()
        cols = ["base", "defc", "sockets", "ms", "life", "res", "rarity", "corrupted",
                "runeforged", "last_price_ex", "min_price_ex", "max_price_ex", "n_sightings",
                "n_reprice_up", "n_reprice_down", "status", "first_seen", "last_seen",
                "exit_price_ex", "ms_explicit", "chaos_res"]
        return [dict(zip(cols, r)) for r in rows]

    def last_poll_ts(self, league: str) -> float | None:
        with sqlite3.connect(self.db_path) as c:
            r = c.execute("SELECT MAX(poll_ts) FROM sightings WHERE league=?", (league,)).fetchone()
        return r[0] if r and r[0] is not None else None

    def summary(self, league: str) -> dict:
        with sqlite3.connect(self.db_path) as c:
            tot = c.execute("SELECT COUNT(*) FROM listings WHERE league=?", (league,)).fetchone()[0]
            active = c.execute("SELECT COUNT(*) FROM listings WHERE league=? AND status='active'", (league,)).fetchone()[0]
            gone = c.execute("SELECT COUNT(*) FROM listings WHERE league=? AND status='gone'", (league,)).fetchone()[0]
            polls = c.execute("SELECT COUNT(DISTINCT poll_ts) FROM sightings WHERE league=?", (league,)).fetchone()[0]
            two = c.execute("SELECT COUNT(*) FROM listings WHERE league=? AND sockets>=2", (league,)).fetchone()[0]
        return {"tracked": tot, "active": active, "gone": gone, "polls": polls, "two_socket": two}


# ---- calibration against real sold history ----------------------------------

def sold_medians(config, defence: tuple[str, ...] | None = None) -> dict[str, list]:
    """Median ACTUAL sold price (ex) per mod_sig, from the user's hideout sales.

    These are ground-truth clearing prices. Tracker exit prices are asking-prices
    at disappearance (an upper bound), so the ratio sold/exit per bucket gives a
    correction factor to turn tracker estimates into real clearing estimates.
    """
    import sqlite3
    import statistics as st
    from .stash import fetch_exalted_rates

    rates, div = fetch_exalted_rates(league=config.league)
    path = Path(config.cache_dir) / "history.db"
    if not path.exists():
        return {}
    groups: dict[str, list] = {}
    with sqlite3.connect(path) as c:
        rows = c.execute(
            "SELECT amount, currency, raw FROM sales WHERE league=? AND "
            "(base_type LIKE '%Boot%' OR base_type LIKE '%Shoe%' OR base_type LIKE '%Sandal%' "
            "OR base_type LIKE '%Slipper%' OR base_type LIKE '%Greaves%' OR base_type LIKE '%Sabaton%')",
            (config.league,)).fetchall()
    for amount, currency, raw in rows:
        try:
            item = json.loads(raw).get("item", {})
        except (ValueError, TypeError):
            continue
        if not item.get("explicitMods"):
            continue  # skip corrupted bricks with no mods
        a = boot_attrs(item)
        if defence and a.defc not in defence:
            continue
        px = _price_ex(amount, currency, rates, div)
        if px > 0:
            groups.setdefault(a.mod_sig, []).append(px)
    return {sig: v for sig, v in groups.items()}


def _correction(exits: dict[str, float], sold: dict[str, float]) -> dict:
    """Pure: pair buckets present in both, compute sold/exit ratios + a pooled
    global factor (geometric mean — robust to the tiny per-bucket samples)."""
    import math

    pairs = []
    for sig in set(exits) & set(sold):
        if exits[sig] > 0:
            pairs.append({"mod_sig": sig, "exit_ex": exits[sig], "sold_ex": sold[sig],
                          "ratio": sold[sig] / exits[sig]})
    if pairs:
        logs = [math.log(p["ratio"]) for p in pairs]
        global_factor = math.exp(sum(logs) / len(logs))
    else:
        global_factor = 1.0
    pairs.sort(key=lambda p: -p["sold_ex"])
    return {"global_factor": global_factor, "n_matched": len(pairs), "pairs": pairs,
            "n_sold_buckets": len(sold), "n_exit_buckets": len(exits)}


def calibrate(config, defence: tuple[str, ...] = FOCUS_DEFENCE) -> dict:
    """Correction factor between tracker exit prices and real sold prices.

    Tracker exit prices are asking-at-disappearance (upper bound); the user's
    hideout sales are ground truth. The report multiplies tracker estimates by
    this factor to show a corrected clearing estimate.
    """
    import statistics as st

    store = TrackerStore(config.cache_dir)
    exits = store.exit_medians(config.league, defence=defence)
    sold = {sig: st.median(v) for sig, v in sold_medians(config, defence=defence).items()}
    return _correction(exits, sold)


# ---- orchestration ----------------------------------------------------------

def _price_ex(amount: float, currency: str, rates: dict, div: float) -> float:
    r = rates.get(currency)
    if r is None:
        r = {"exalted": 1.0, "divine": div, "chaos": rates.get("chaos", 12.0)}.get(currency, 0.0)
    return (amount or 0.0) * r


def run_poll(config, api=None, profiles=None, poll_ts: float | None = None) -> PollResult:
    """Poll every profile once, store OFFLINE listings, age out the rest.

    ``poll_ts`` is injectable for tests; defaults to wall-clock time.
    """
    from .api import TradeAPI
    from .stash import fetch_exalted_rates

    api = api or TradeAPI(config)
    profiles = profiles or DEFAULT_PROFILES
    league = config.league
    store = TrackerStore(config.cache_dir)
    rates, div = fetch_exalted_rates(league=league)
    if poll_ts is None:
        # tests pass poll_ts explicitly; only touch the clock in real runs.
        poll_ts = time.time()

    result = PollResult(league=league, poll_ts=poll_ts)
    all_seen: set[str] = set()
    for prof in profiles:
        rows, offline, total, pages = _enumerate_profile(api, prof, rates, div)
        for item_hash, *_ in rows:
            all_seen.add(item_hash)
        store.record_offline(league, rows, poll_ts, result)
        result.offline += offline
        result.by_profile[prof.name] = {"total_listed": total, "pages": pages,
                                        "offline_stored": offline}
    store.mark_absent(league, all_seen, poll_ts, result)
    return result


def _enumerate_profile(api, prof: Profile, rates: dict, div: float):
    """Price-walk a profile to enumerate its full OFFLINE cohort.

    The search endpoint returns at most 100 hashes, so we page by raising the
    price floor to the highest price seen on the previous page, de-duping by item
    hash, until a page returns <100 results (cohort exhausted) or we hit
    ``max_pages``. This yields a stable, price-defined cohort instead of an
    unstable "cheapest 100 of thousands" sample.
    """
    rows = []
    seen: set[str] = set()
    floor = prof.min_div
    total0 = 0
    pages = 0
    for _page in range(prof.max_pages):
        qid, ids, total = api.search(prof.query(floor_div=floor))
        if pages == 0:
            total0 = total
        listings = api.fetch(ids[:100], qid) if ids else []
        pages += 1
        page_max = floor
        for r in listings:
            item, lst = r.get("item", {}), r.get("listing", {})
            item_hash = r.get("id")
            if not item_hash:
                continue
            price = lst.get("price") or {}
            px = _price_ex(price.get("amount"), price.get("currency", ""), rates, div)
            page_max = max(page_max, px / div if div else 0.0)
            if item_hash in seen:
                continue
            if (lst.get("account") or {}).get("online"):
                continue  # OFFLINE-ONLY: skip listings whose seller is online
            attrs = boot_attrs(item)
            if attrs.defc not in FOCUS_DEFENCE:
                continue  # ignore anything with Armour (and unknown defence)
            seen.add(item_hash)
            rows.append((item_hash, attrs, px, price.get("amount") or 0.0,
                         price.get("currency", ""), lst.get("indexed", "")))
        if len(ids) < 100 or floor >= prof.max_div:
            break  # exhausted this cohort / hit the price cap
        # advance the floor; guard against a tie-cluster stalling the walk
        floor = page_max + 0.01 if page_max <= floor + 1e-9 else page_max
    return rows, len(rows), total0, pages


def print_report(config, defence=FOCUS_DEFENCE, div: float | None = None) -> None:
    from .stash import fetch_exalted_rates
    store = TrackerStore(config.cache_dir)
    league = config.league
    if div is None:
        _, div = fetch_exalted_rates(league=league)
    s = store.summary(league)
    print(f"== tracker [{league}] ==  tracked={s['tracked']} active={s['active']} "
          f"gone={s['gone']} 2-socket={s['two_socket']} polls={s['polls']}  (1 div={div:.0f} ex)")
    # The raw exit_median (price at disappearance) has empirically matched actual
    # sales within ~10% (validated 2026-06-15 on 3 sales), so it IS the estimate.
    # The historical sold/exit ratio is shown only as a diagnostic — it skews low
    # because past sales were underpriced/softer, and applying it hurt accuracy.
    cal = calibrate(config, defence=defence)
    if cal["n_matched"]:
        print(f"(diagnostic: your historical sold/exit ratio was x{cal['global_factor']:.2f} "
              f"over {cal['n_matched']} buckets — NOT applied; raw exit tracks recent sales better)")
    rep = store.clearing_report(league, defence=defence)
    if not rep:
        print("\nNo cleared listings yet — needs more polls. (A listing must vanish "
              f"for {GONE_AFTER} consecutive polls to count as a sale.)")
    else:
        print(f"\n-- inferred CLEARING prices by mod-bucket (defence {defence}) --")
        print(f"{'mod_sig':<46} {'n':>3} {'clearing':>9} {'p25-p75':>14} {'days':>5} {'cut%':>5}")
        for r in rep:
            print(f"{r['mod_sig']:<46} {r['n_exits']:>3} "
                  f"{r['median_exit_ex']/div:>7.1f}d "
                  f"{r['p25_exit_ex']/div:>5.1f}-{r['p75_exit_ex']/div:<6.1f}d "
                  f"{r['median_days_on_market']:>5.1f} {r['reprice_down_rate']*100:>4.0f}%")
    stale = store.stale_ceilings(league, defence=defence)
    if stale:
        print(f"\n-- stale listings (sitting unsold >=3d = soft ceilings) --")
        for r in stale[:15]:
            print(f"{r['mod_sig']:<46} asking {r['price_ex']/div:>6.1f}d  "
                  f"age {r['age_days']:>4}d  cuts={r['reprice_downs']}")


def _print_poll(res: PollResult) -> None:
    print(f"poll @ {time.strftime('%H:%M:%S', time.localtime(res.poll_ts))} | "
          f"offline={res.offline} new={res.new} reprice_up={res.repriced_up} "
          f"reprice_down={res.repriced_down} marked_gone={res.marked_gone}", flush=True)
    for name, s in res.by_profile.items():
        print(f"  {name}: total_listed={s['total_listed']} pages={s['pages']} "
              f"offline_enumerated={s['offline_stored']}", flush=True)


def recently_polled(config, interval_min: float) -> bool:
    """True if some instance polled within 60% of the interval — lets a standalone
    watcher and the web-server watcher coexist without double-polling."""
    last = TrackerStore(config.cache_dir).last_poll_ts(config.league)
    return last is not None and (time.time() - last) < interval_min * 60 * 0.6


def watch(config, interval_min: float = 13.0) -> None:
    """Recurring poller: poll, then sleep, forever. Ctrl-C to stop."""
    print(f"tracker watch: polling every {interval_min:.0f} min (Ctrl-C to stop)", flush=True)
    while True:
        try:
            if recently_polled(config, interval_min):
                print("  (skip: another instance polled recently)", flush=True)
            else:
                _print_poll(run_poll(config))
        except Exception as e:  # never let one bad poll kill the loop
            print(f"  !! poll error: {e}", flush=True)
        time.sleep(interval_min * 60)


if __name__ == "__main__":
    import sys
    from .config import Config
    cfg = Config.load()
    action = sys.argv[1] if len(sys.argv) > 1 else "poll"
    if action == "poll":
        _print_poll(run_poll(cfg))
    elif action == "report":
        print_report(cfg)
    elif action == "watch":
        watch(cfg, float(sys.argv[2]) if len(sys.argv) > 2 else 13.0)
    else:
        print(f"unknown action {action!r}; use 'poll', 'report', or 'watch [minutes]'")
