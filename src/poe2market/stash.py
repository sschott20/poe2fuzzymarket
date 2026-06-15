"""Stash reading and currency net-worth valuation.

Reading your own stash uses the undocumented legacy character-window endpoint
with ``realm=poe2``, authenticated by the same POESESSID cookie used elsewhere
(no OAuth, no public tab required). Pricing uses poe2scout's free public API,
which quotes PoE2 currency in Exalted Orbs (the league base unit) and gives the
Divine/Exalted ratio for divine-equivalent totals.
"""

import json
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

POE2SCOUT_BASE = "https://api.poe2scout.com/api/poe2"
# Categories worth valuing in a currency-heavy stash. "currency" covers orbs;
# the rest catch common stackable valuables that live in currency tabs.
PRICE_CATEGORIES = ["currency", "fragments", "runes", "essences", "catalysts"]


def parse_stack_size(item: dict) -> int:
    """Quantity of a (stackable) item. Prefers ``stackSize``; falls back to the
    'Stack Size' property string like ``"742/5000"``."""
    if isinstance(item.get("stackSize"), int):
        return item["stackSize"]
    for prop in item.get("properties", []) or []:
        if prop.get("name") in ("Stack Size", "Stack"):
            values = prop.get("values") or []
            if values and values[0]:
                head = str(values[0][0]).split("/", 1)[0].replace(",", "")
                try:
                    return int(head)
                except ValueError:
                    return 0
    return 1  # non-stackable item present => quantity 1


@dataclass
class Holding:
    name: str
    quantity: int
    unit_price_ex: float  # exalted-equivalent per unit (0 if unpriced)
    icon: str = ""

    @property
    def total_ex(self) -> float:
        return self.quantity * self.unit_price_ex


@dataclass
class NetWorth:
    league: str = ""
    total_exalted: float = 0.0
    total_divine: float = 0.0
    divine_price: float = 0.0  # exalted per divine
    holdings: list[Holding] = field(default_factory=list)
    unpriced: list[dict] = field(default_factory=list)  # name + quantity
    tabs_valued: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "league": self.league,
            "total_exalted": round(self.total_exalted, 2),
            "total_divine": round(self.total_divine, 3),
            "divine_price": round(self.divine_price, 2),
            "holdings": [
                {
                    "name": h.name,
                    "quantity": h.quantity,
                    "unit_price_ex": round(h.unit_price_ex, 3),
                    "total_ex": round(h.total_ex, 2),
                    "total_div": round(h.total_ex / self.divine_price, 3)
                    if self.divine_price
                    else 0.0,
                    "share": round(h.total_ex / self.total_exalted, 4)
                    if self.total_exalted
                    else 0.0,
                    "icon": h.icon,
                }
                for h in self.holdings
            ],
            "unpriced": self.unpriced,
            "tabs_valued": self.tabs_valued,
        }


def fetch_currency_prices(
    user_agent: str = "poe2fuzzymarket/1.0",
    league: str = "Runes of Aldur",
) -> tuple[dict[str, dict], float]:
    """Fetch a ``{base_type_lower: {price, icon}}`` map and the divine price.

    Prices are in Exalted Orbs. Returns ``(price_map, divine_price_ex)``.
    """
    headers = {"User-Agent": user_agent, "Accept": "application/json"}
    price_map: dict[str, dict] = {}
    divine_price = 0.0

    with httpx.Client(timeout=25.0, headers=headers) as client:
        # League metadata -> divine price (exalted per divine)
        resp = client.get(f"{POE2SCOUT_BASE}/Leagues")
        resp.raise_for_status()
        data = resp.json()
        leagues = data if isinstance(data, list) else data.get("Leagues", [])
        for lg in leagues:
            if str(lg.get("Value")) == league:
                divine_price = float(lg.get("DivinePrice") or 0) or 0.0
                break

        # Currency prices across the relevant categories
        from urllib.parse import quote

        lg_enc = quote(league)
        for category in PRICE_CATEGORIES:
            try:
                r = client.get(
                    f"{POE2SCOUT_BASE}/Leagues/{lg_enc}/Currencies/ByCategory",
                    params={"Category": category},
                )
                if r.status_code != 200:
                    continue
                payload = r.json()
            except (httpx.HTTPError, ValueError):
                continue
            for it in payload.get("Items", []) or []:
                meta = it.get("ItemMetadata") or {}
                name = meta.get("base_type") or it.get("Text") or ""
                price = it.get("CurrentPrice")
                if not name or price is None:
                    continue
                key = name.lower()
                # Keep the first (highest-confidence) price we see per name.
                price_map.setdefault(
                    key,
                    {"price": float(price), "icon": meta.get("icon", "") or ""},
                )

    return price_map, divine_price


def fetch_exalted_rates(
    user_agent: str = "poe2fuzzymarket/1.0",
    league: str = "Runes of Aldur",
) -> tuple[dict[str, float], float]:
    """Live currency rates keyed by trade currency id, in Exalted-equivalent.

    Returns ``(rates, divine_price)`` where ``rates[id]`` is the exalted value
    of one of that currency (``exalted`` is always 1.0) and ``divine_price`` is
    exalted-per-divine. Keys are poe2scout ``ApiId`` values (e.g. ``"divine"``,
    ``"chaos"``, ``"vaal"``), which match the ``price.currency`` tokens the trade
    API returns. Falls back to static rates on any failure.
    """
    from .scorer import DEFAULT_EXALTED_RATES

    headers = {"User-Agent": user_agent, "Accept": "application/json"}
    rates: dict[str, float] = {"exalted": 1.0}
    divine_price = 0.0
    try:
        from urllib.parse import quote

        lg_enc = quote(league)
        with httpx.Client(timeout=25.0, headers=headers) as client:
            resp = client.get(f"{POE2SCOUT_BASE}/Leagues")
            resp.raise_for_status()
            data = resp.json()
            leagues = data if isinstance(data, list) else data.get("Leagues", [])
            for lg in leagues:
                if str(lg.get("Value")) == league:
                    divine_price = float(lg.get("DivinePrice") or 0) or 0.0
                    break
            for category in PRICE_CATEGORIES:
                try:
                    r = client.get(
                        f"{POE2SCOUT_BASE}/Leagues/{lg_enc}/Currencies/ByCategory",
                        params={"Category": category},
                    )
                    if r.status_code != 200:
                        continue
                    payload = r.json()
                except (httpx.HTTPError, ValueError):
                    continue
                for it in payload.get("Items", []) or []:
                    api_id = it.get("ApiId")
                    price = it.get("CurrentPrice")
                    if api_id and price is not None:
                        rates.setdefault(api_id, float(price))
    except httpx.HTTPError:
        pass

    # Fill any gaps from the static table so valuation never silently drops a
    # currency the live feed didn't list.
    for k, v in DEFAULT_EXALTED_RATES.items():
        rates.setdefault(k, v)
    if not divine_price:
        divine_price = rates.get("divine", DEFAULT_EXALTED_RATES["divine"])
    rates["divine"] = divine_price  # keep the authoritative league ratio
    return rates, divine_price


def value_currency(
    items: list[dict], price_map: dict[str, dict], divine_price: float
) -> tuple[list[Holding], list[dict]]:
    """Aggregate stash items into priced holdings + an unpriced remainder."""
    qty_by_name: dict[str, int] = {}
    icon_by_name: dict[str, str] = {}
    for item in items:
        name = item.get("baseType") or item.get("typeLine") or ""
        if not name:
            continue
        qty = parse_stack_size(item)
        qty_by_name[name] = qty_by_name.get(name, 0) + qty
        if item.get("icon"):
            icon_by_name.setdefault(name, item["icon"])

    holdings: list[Holding] = []
    unpriced: list[dict] = []
    for name, qty in qty_by_name.items():
        entry = price_map.get(name.lower())
        if entry is None:
            unpriced.append({"name": name, "quantity": qty})
            continue
        holdings.append(
            Holding(
                name=name,
                quantity=qty,
                unit_price_ex=entry["price"],
                icon=icon_by_name.get(name) or entry.get("icon", ""),
            )
        )

    holdings.sort(key=lambda h: h.total_ex, reverse=True)
    unpriced.sort(key=lambda u: u["quantity"], reverse=True)
    return holdings, unpriced


def compute_net_worth(
    league: str,
    stash_items: list[dict],
    tabs_valued: list[str],
    user_agent: str = "poe2fuzzymarket/1.0",
) -> NetWorth:
    price_map, divine_price = fetch_currency_prices(user_agent, league)
    holdings, unpriced = value_currency(stash_items, price_map, divine_price)
    total_ex = sum(h.total_ex for h in holdings)
    return NetWorth(
        league=league,
        total_exalted=total_ex,
        total_divine=(total_ex / divine_price) if divine_price else 0.0,
        divine_price=divine_price,
        holdings=holdings,
        unpriced=unpriced,
        tabs_valued=tabs_valued,
    )


# ── net-worth-over-time snapshots ─────────────────────────────────────────


class NetWorthStore:
    """Stores one snapshot per refresh, to chart net worth over time."""

    def __init__(self, cache_dir: str):
        self.db_path = Path(cache_dir) / "networth.db"
        Path(cache_dir).mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS snapshots (
                    league         TEXT NOT NULL,
                    ts             REAL NOT NULL,
                    total_exalted  REAL,
                    total_divine   REAL,
                    divine_price   REAL,
                    payload        TEXT,
                    PRIMARY KEY (league, ts)
                )
                """
            )

    def add(self, net_worth: NetWorth, ts: float) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO snapshots "
                "(league, ts, total_exalted, total_divine, divine_price, payload) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    net_worth.league,
                    ts,
                    net_worth.total_exalted,
                    net_worth.total_divine,
                    net_worth.divine_price,
                    json.dumps(net_worth.to_dict()),
                ),
            )

    def series(self, league: str) -> list[dict]:
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT ts, total_exalted, total_divine FROM snapshots "
                "WHERE league = ? ORDER BY ts",
                (league,),
            ).fetchall()
        return [
            {"ts": r[0], "exalted": r[1], "divine": r[2]} for r in rows
        ]

    def latest(self, league: str) -> dict | None:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT payload, ts FROM snapshots WHERE league = ? "
                "ORDER BY ts DESC LIMIT 1",
                (league,),
            ).fetchone()
        if not row:
            return None
        data = json.loads(row[0])
        data["ts"] = row[1]
        return data

    def clear(self, league: str | None = None) -> None:
        with sqlite3.connect(self.db_path) as conn:
            if league is None:
                conn.execute("DELETE FROM snapshots")
            else:
                conn.execute("DELETE FROM snapshots WHERE league = ?", (league,))
