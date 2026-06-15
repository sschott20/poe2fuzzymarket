"""Persistent store of observed trade listings.

Every item fetched during a deals/analyze search is saved here, deduped by its
listing id. The trade API only returns ~100 results per search, so a single
query is a small snapshot; by accumulating every fetch over time the regression
gets a much larger, richer sample to learn from. Listings are tagged with the
base defence profile (armour/evasion/energy-shield) so saved data can still be
filtered by attribute combo.
"""

import json
import sqlite3
import time
from pathlib import Path

from .api import parse_listing
from .models import Listing, StatValue


def defence_flags(item: dict) -> dict[str, int]:
    """Detect which base defences an item has, from its properties."""
    flags = {"ar": 0, "ev": 0, "es": 0}
    for prop in item.get("properties", []) or []:
        low = str(prop.get("name", "")).lower()
        # independent ifs (not elif): a hybrid property naming two defences sets both
        if "armour" in low:
            flags["ar"] = 1
        if "evasion" in low:
            flags["ev"] = 1
        if "energy shield" in low or "energyshield" in low:
            flags["es"] = 1
    return flags


class ObservationStore:
    """SQLite store of fetched listings, accumulated across searches."""

    def __init__(self, cache_dir: str):
        self.db_path = Path(cache_dir) / "observations.db"
        Path(cache_dir).mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS observations (
                    item_id   TEXT NOT NULL,
                    league    TEXT NOT NULL,
                    category  TEXT,
                    base_type TEXT,
                    name      TEXT,
                    rarity    TEXT,
                    ilvl      INTEGER,
                    amount    REAL,
                    currency  TEXT,
                    has_ar    INTEGER,
                    has_ev    INTEGER,
                    has_es    INTEGER,
                    stats     TEXT,
                    ts        REAL,
                    PRIMARY KEY (league, item_id)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_obs_cat ON observations (league, category)"
            )

    def record(self, league: str, category: str, raw_items: list[dict], ts: float) -> int:
        """Upsert fetched listings. Returns the number of new (unseen) rows."""
        rows = []
        for raw in raw_items:
            listing = parse_listing(raw)
            if not listing.item_id:
                continue
            flags = defence_flags(raw.get("item", {}) or {})
            stats_json = json.dumps(
                [{"id": s.stat_id, "text": s.text, "value": s.value} for s in listing.stats]
            )
            rows.append(
                (
                    listing.item_id, league, category, listing.base_type,
                    listing.name, "", listing.ilvl, listing.price, listing.currency,
                    flags["ar"], flags["ev"], flags["es"], stats_json, ts,
                )
            )
        if not rows:
            return 0
        ids = [r[0] for r in rows]
        with sqlite3.connect(self.db_path) as conn:
            placeholders = ",".join("?" * len(ids))
            existing = conn.execute(
                f"SELECT COUNT(*) FROM observations "
                f"WHERE league = ? AND item_id IN ({placeholders})",
                [league, *ids],
            ).fetchone()[0]
            # Refresh price/ts on re-observation; INSERT OR REPLACE keeps one row per id.
            conn.executemany(
                """
                INSERT OR REPLACE INTO observations (
                    item_id, league, category, base_type, name, rarity, ilvl,
                    amount, currency, has_ar, has_ev, has_es, stats, ts
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            return len(rows) - existing

    def query(
        self,
        league: str,
        category: str,
        attributes: list[str] | None = None,
        limit: int = 5000,
    ) -> list[Listing]:
        """Return stored listings for a category, optionally filtered by the
        same attribute-combo logic as a live search."""
        sql = "SELECT amount, currency, ilvl, base_type, name, stats FROM observations WHERE league = ? AND category = ?"
        params: list = [league, category]
        if attributes:
            attrs = {a.lower() for a in attributes}
            for attr, col in (("str", "has_ar"), ("dex", "has_ev"), ("int", "has_es")):
                sql += f" AND {col} = ?"
                params.append(1 if attr in attrs else 0)
        sql += " ORDER BY ts DESC LIMIT ?"
        params.append(limit)
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(sql, params).fetchall()

        listings: list[Listing] = []
        for amount, currency, ilvl, base_type, name, stats_json in rows:
            try:
                stats = [
                    StatValue(stat_id=s["id"], text=s["text"], value=s["value"])
                    for s in json.loads(stats_json or "[]")
                ]
            except (ValueError, TypeError, KeyError):
                stats = []
            listings.append(
                Listing(
                    item_id="",
                    price=amount or 0.0,
                    currency=currency or "",
                    stats=stats,
                    name=name or "",
                    base_type=base_type or "",
                    ilvl=ilvl or 0,
                )
            )
        return listings

    def category_counts(self, league: str) -> list[dict]:
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT category, COUNT(*) FROM observations WHERE league = ? "
                "GROUP BY category ORDER BY COUNT(*) DESC",
                (league,),
            ).fetchall()
        return [{"category": r[0], "count": r[1]} for r in rows]

    def count(self, league: str, category: str | None = None) -> int:
        with sqlite3.connect(self.db_path) as conn:
            if category:
                return conn.execute(
                    "SELECT COUNT(*) FROM observations WHERE league = ? AND category = ?",
                    (league, category),
                ).fetchone()[0]
            return conn.execute(
                "SELECT COUNT(*) FROM observations WHERE league = ?", (league,)
            ).fetchone()[0]

    def clear(self, league: str | None = None) -> None:
        with sqlite3.connect(self.db_path) as conn:
            if league is None:
                conn.execute("DELETE FROM observations")
            else:
                conn.execute("DELETE FROM observations WHERE league = ?", (league,))
