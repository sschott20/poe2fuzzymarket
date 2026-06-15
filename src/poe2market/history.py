"""Merchant (sale) history: fetching, local persistence, and aggregation.

PoE2 exposes your last ~100 hideout-merchant sales at the undocumented but
official endpoint ``GET /api/trade2/history/{league}`` (POESESSID-authenticated).
The server only keeps a short rolling window, so we persist every sync into a
local SQLite store and dedupe by sale id. Sync regularly and history accumulates
well beyond the server's window.
"""

import json
import re
import sqlite3
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .scorer import DEFAULT_EXALTED_RATES, normalize_price

# PoE item text embeds glossary markup like "[Resistances|Fire Resistance]"
# (show the right side) and "[Corrupted]" (show as-is). Strip it for display.
_MARKUP_RE = re.compile(r"\[(?:[^\[\]]*\|)?([^\[\]]+)\]")


def clean_markup(text: str) -> str:
    return _MARKUP_RE.sub(r"\1", text)


def _clean_list(mods: list | None) -> list[str]:
    return [clean_markup(str(m)) for m in (mods or [])]


def _format_properties(props: list | None) -> list[str]:
    """Render item properties as readable 'Name: value' strings."""
    out: list[str] = []
    for prop in props or []:
        name = clean_markup(str(prop.get("name", "")))
        values = prop.get("values") or []
        vals = ", ".join(clean_markup(str(v[0])) for v in values if v)
        out.append(f"{name}: {vals}" if vals else name)
    return out


@dataclass
class Sale:
    """A single completed sale from the merchant history."""

    sale_id: str
    time: str = ""  # ISO 8601 timestamp from the API
    amount: float = 0.0
    currency: str = ""
    name: str = ""
    base_type: str = ""
    rarity: str = ""
    ilvl: int = 0
    stack_size: int = 0
    icon: str = ""
    implicit_mods: list[str] = field(default_factory=list)
    explicit_mods: list[str] = field(default_factory=list)
    rune_mods: list[str] = field(default_factory=list)
    enchant_mods: list[str] = field(default_factory=list)
    fractured_mods: list[str] = field(default_factory=list)
    properties: list[str] = field(default_factory=list)
    requirements: list[str] = field(default_factory=list)

    def exalted_value(self, rates: dict[str, float] | None = None) -> float:
        return normalize_price(self.amount, self.currency, rates or DEFAULT_EXALTED_RATES)


def _sale_key(raw: dict, item: dict, price: dict) -> str:
    """Stable unique key for a sale, even when the API omits an explicit id."""
    explicit = raw.get("item_id") or raw.get("id") or item.get("id")
    if explicit:
        return str(explicit)
    # Fall back to a content hash of the fields that identify the transaction.
    parts = [
        str(raw.get("time") or raw.get("indexed") or ""),
        str(item.get("name", "")),
        str(item.get("typeLine") or item.get("baseType") or ""),
        str(price.get("amount", "")),
        str(price.get("currency", "")),
    ]
    return "syn:" + "|".join(parts)


def parse_sale(raw: dict) -> Sale:
    """Parse one raw merchant-history entry into a :class:`Sale`.

    Tolerant of the two shapes the endpoint has used: a flat ``{time, price,
    item}`` entry and a trade-style ``{listing: {price, indexed}, item}`` entry.
    """
    item = raw.get("item", {}) or {}
    listing = raw.get("listing", {}) or {}
    price = raw.get("price") or listing.get("price") or {}

    stack = 0
    for prop in item.get("properties", []) or []:
        if prop.get("name") in ("Stack Size", "Stack"):
            values = prop.get("values") or []
            if values and values[0]:
                # e.g. "742/5000" -> 742
                head = str(values[0][0]).split("/", 1)[0].replace(",", "")
                try:
                    stack = int(head)
                except ValueError:
                    pass

    return Sale(
        sale_id=_sale_key(raw, item, price),
        time=raw.get("time") or raw.get("indexed") or listing.get("indexed") or "",
        amount=float(price.get("amount", 0) or 0),
        currency=price.get("currency", "") or "",
        name=clean_markup(item.get("name", "") or ""),
        base_type=clean_markup(item.get("baseType") or item.get("typeLine", "") or ""),
        rarity=item.get("rarity", "") or "",
        ilvl=int(item.get("ilvl", 0) or 0),
        stack_size=stack,
        icon=item.get("icon", "") or "",
        implicit_mods=_clean_list(item.get("implicitMods")),
        explicit_mods=_clean_list(item.get("explicitMods")),
        rune_mods=_clean_list(item.get("runeMods")),
        enchant_mods=_clean_list(item.get("enchantMods")),
        fractured_mods=_clean_list(item.get("fracturedMods")),
        properties=_format_properties(item.get("properties")),
        requirements=_format_properties(item.get("requirements")),
    )


class HistoryStore:
    """SQLite-backed, deduplicated store of sales, partitioned by league."""

    def __init__(self, cache_dir: str):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.cache_dir / "history.db"
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sales (
                    sale_id    TEXT NOT NULL,
                    league     TEXT NOT NULL,
                    time       TEXT,
                    amount     REAL,
                    currency   TEXT,
                    name       TEXT,
                    base_type  TEXT,
                    rarity     TEXT,
                    ilvl       INTEGER,
                    stack_size INTEGER,
                    icon       TEXT,
                    raw        TEXT,
                    PRIMARY KEY (league, sale_id)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_sales_league_time "
                "ON sales (league, time)"
            )

    def upsert_many(self, league: str, raw_entries: list[dict]) -> int:
        """Insert parsed sales, ignoring ones already stored.

        Returns the number of genuinely new sales added.
        """
        if not raw_entries:
            return 0
        rows = []
        for raw in raw_entries:
            sale = parse_sale(raw)
            if not sale.sale_id:
                continue
            rows.append(
                (
                    sale.sale_id,
                    league,
                    sale.time,
                    sale.amount,
                    sale.currency,
                    sale.name,
                    sale.base_type,
                    sale.rarity,
                    sale.ilvl,
                    sale.stack_size,
                    sale.icon,
                    json.dumps(raw),
                )
            )
        with sqlite3.connect(self.db_path) as conn:
            before = conn.total_changes
            conn.executemany(
                """
                INSERT OR IGNORE INTO sales (
                    sale_id, league, time, amount, currency, name, base_type,
                    rarity, ilvl, stack_size, icon, raw
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            return conn.total_changes - before

    def all(self, league: str) -> list[Sale]:
        """Return every stored sale for a league, newest first.

        Sales are reconstructed from the persisted raw JSON via :func:`parse_sale`
        so full item detail (mods, properties) is available, not just the indexed
        columns.
        """
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "SELECT raw, sale_id, time, amount, currency, name, base_type, "
                "rarity, ilvl, stack_size, icon FROM sales WHERE league = ? "
                "ORDER BY time DESC",
                (league,),
            )
            rows = cursor.fetchall()

        sales: list[Sale] = []
        for r in rows:
            raw_json = r[0]
            if raw_json:
                try:
                    sales.append(parse_sale(json.loads(raw_json)))
                    continue
                except (ValueError, TypeError):
                    pass
            # Fallback to indexed columns if raw is missing/corrupt.
            sales.append(
                Sale(
                    sale_id=r[1],
                    time=r[2] or "",
                    amount=r[3] or 0.0,
                    currency=r[4] or "",
                    name=r[5] or "",
                    base_type=r[6] or "",
                    rarity=r[7] or "",
                    ilvl=r[8] or 0,
                    stack_size=r[9] or 0,
                    icon=r[10] or "",
                )
            )
        return sales

    def count(self, league: str) -> int:
        with sqlite3.connect(self.db_path) as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM sales WHERE league = ?", (league,)
            ).fetchone()[0]

    def leagues(self) -> list[str]:
        with sqlite3.connect(self.db_path) as conn:
            return [
                r[0]
                for r in conn.execute(
                    "SELECT DISTINCT league FROM sales ORDER BY league"
                ).fetchall()
            ]

    def clear(self, league: str | None = None) -> None:
        with sqlite3.connect(self.db_path) as conn:
            if league is None:
                conn.execute("DELETE FROM sales")
            else:
                conn.execute("DELETE FROM sales WHERE league = ?", (league,))


# ── aggregation ──────────────────────────────────────────────────────────


@dataclass
class HistorySummary:
    count: int = 0
    first_sale: str = ""
    last_sale: str = ""
    total_exalted: float = 0.0
    total_divine: float = 0.0
    divine_price: float = 0.0  # exalted per divine
    avg_exalted: float = 0.0
    median_exalted: float = 0.0
    max_sale_exalted: float = 0.0
    max_sale_label: str = ""
    by_currency: list[dict] = field(default_factory=list)
    by_rarity: list[dict] = field(default_factory=list)
    cumulative: list[dict] = field(default_factory=list)
    daily: list[dict] = field(default_factory=list)
    rates: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2.0


def summarize(
    sales: list[Sale], rates: dict[str, float] | None = None
) -> HistorySummary:
    """Compute dashboard aggregates from a list of sales.

    Totals are expressed in Exalted-equivalent (and Divine-equivalent) using the
    supplied rates (Exalted = 1). With live poe2scout rates these reflect the
    current PoE2 economy; the static fallback is approximate.
    """
    r = rates or DEFAULT_EXALTED_RATES
    divine_price = r.get("divine", 1.0) or 1.0
    summary = HistorySummary(rates=dict(r), divine_price=divine_price)
    if not sales:
        return summary

    # Chronological order for cumulative / daily series.
    chrono = sorted(sales, key=lambda s: s.time)
    values = [s.exalted_value(r) for s in chrono]

    summary.count = len(sales)
    summary.first_sale = chrono[0].time
    summary.last_sale = chrono[-1].time
    summary.total_exalted = sum(values)
    summary.total_divine = summary.total_exalted / divine_price
    summary.avg_exalted = summary.total_exalted / summary.count
    summary.median_exalted = _median(values)

    # Largest single sale.
    max_idx = max(range(len(chrono)), key=lambda i: values[i])
    top = chrono[max_idx]
    summary.max_sale_exalted = values[max_idx]
    summary.max_sale_label = top.name or top.base_type or "?"

    # Breakdown by raw currency received.
    cur: dict[str, dict] = {}
    for s, v in zip(chrono, values):
        c = cur.setdefault(
            s.currency or "unknown",
            {"currency": s.currency or "unknown", "count": 0, "amount": 0.0, "exalted": 0.0},
        )
        c["count"] += 1
        c["amount"] += s.amount
        c["exalted"] += v
    summary.by_currency = sorted(
        cur.values(), key=lambda x: x["exalted"], reverse=True
    )

    # Breakdown by item rarity.
    rar: dict[str, dict] = {}
    for s, v in zip(chrono, values):
        key = (s.rarity or "unknown").lower()
        c = rar.setdefault(key, {"rarity": key, "count": 0, "exalted": 0.0})
        c["count"] += 1
        c["exalted"] += v
    summary.by_rarity = sorted(rar.values(), key=lambda x: x["exalted"], reverse=True)

    # Cumulative "net worth" (realized income) over time.
    running = 0.0
    cumulative = []
    for s, v in zip(chrono, values):
        running += v
        if s.time:
            cumulative.append({"t": s.time, "exalted": round(running, 2)})
    summary.cumulative = cumulative

    # Daily totals (date string -> exalted + count).
    daily: dict[str, dict] = {}
    for s, v in zip(chrono, values):
        if not s.time:
            continue
        day = s.time[:10]  # YYYY-MM-DD prefix of ISO timestamp
        d = daily.setdefault(day, {"date": day, "exalted": 0.0, "count": 0})
        d["exalted"] += v
        d["count"] += 1
    summary.daily = [
        {"date": k, "exalted": round(v["exalted"], 2), "count": v["count"]}
        for k, v in sorted(daily.items())
    ]

    return summary
