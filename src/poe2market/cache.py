import json
import sqlite3
import time
from pathlib import Path


class Cache:
    """Simple SQLite-backed key-value cache with TTL expiry."""

    def __init__(self, cache_dir: str, ttl_hours: int = 24):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.cache_dir / "cache.db"
        self.ttl_seconds = ttl_hours * 3600
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS cache (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    timestamp REAL NOT NULL
                )
                """
            )

    def get(self, key: str) -> dict | list | None:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT value, timestamp FROM cache WHERE key = ?", (key,)
            ).fetchone()
            if row is None:
                return None
            value, ts = row
            if time.time() - ts > self.ttl_seconds:
                conn.execute("DELETE FROM cache WHERE key = ?", (key,))
                return None
            return json.loads(value)

    def set(self, key: str, value: dict | list) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO cache (key, value, timestamp) VALUES (?, ?, ?)",
                (key, json.dumps(value), time.time()),
            )

    def clear(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM cache")

    def prune_expired(self) -> int:
        """Remove all expired entries. Returns count of deleted rows."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "DELETE FROM cache WHERE ? - timestamp > ?",
                (time.time(), self.ttl_seconds),
            )
            return cursor.rowcount
