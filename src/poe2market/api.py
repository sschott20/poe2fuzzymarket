import re
import time

import httpx

from .config import Config
from .models import Listing, StatFilter, StatValue

BASE_URL = "https://www.pathofexile.com"
API_PREFIX = "/api/trade2"

SEARCH_RATE_LIMIT = (5, 12)
FETCH_RATE_LIMIT = (12, 6)


class RateLimiter:
    """Sliding-window rate limiter that respects API response headers."""

    def __init__(self, max_requests: int, per_seconds: float):
        self.max_requests = max_requests
        self.per_seconds = per_seconds
        self._timestamps: list[float] = []

    def wait(self) -> None:
        now = time.monotonic()
        self._timestamps = [
            t for t in self._timestamps if now - t < self.per_seconds
        ]
        if len(self._timestamps) >= self.max_requests:
            sleep_time = self._timestamps[0] + self.per_seconds - now
            if sleep_time > 0:
                time.sleep(sleep_time)
        self._timestamps.append(time.monotonic())

    def update_from_headers(self, headers: httpx.Headers) -> None:
        for key, value in headers.items():
            k = key.lower()
            if (
                k.startswith("x-rate-limit-")
                and k not in ("x-rate-limit-policy", "x-rate-limit-rules")
                and not k.endswith("-state")
            ):
                parts = value.split(",")[0].split(":")
                if len(parts) >= 2:
                    try:
                        self.max_requests = int(parts[0])
                        self.per_seconds = float(parts[1])
                    except ValueError:
                        pass


class TradeAPI:
    def __init__(self, config: Config):
        self.config = config
        self._search_rl = RateLimiter(*SEARCH_RATE_LIMIT)
        self._fetch_rl = RateLimiter(*FETCH_RATE_LIMIT)
        self._stats_cache: list[dict] | None = None
        cookies = {"POESESSID": config.poesessid} if config.poesessid else {}
        self.client = httpx.Client(
            base_url=BASE_URL,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "X-Requested-With": "XMLHttpRequest",
                "User-Agent": "poe2market/0.1",
            },
            cookies=cookies,
            timeout=30.0,
        )

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> "TradeAPI":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    # -- data endpoints --

    def get_leagues(self) -> list[dict]:
        resp = self.client.get(f"{API_PREFIX}/data/leagues")
        resp.raise_for_status()
        return resp.json()["result"]

    def get_stats(self) -> list[dict]:
        if self._stats_cache is not None:
            return self._stats_cache
        resp = self.client.get(f"{API_PREFIX}/data/stats")
        resp.raise_for_status()
        self._stats_cache = resp.json()["result"]
        return self._stats_cache

    def get_items(self) -> list[dict]:
        resp = self.client.get(f"{API_PREFIX}/data/items")
        resp.raise_for_status()
        return resp.json()["result"]

    # -- search / fetch --

    def search(self, query: dict) -> tuple[str, list[str], int]:
        """Run a trade search. Returns (query_id, result_hashes, total)."""
        self._search_rl.wait()
        resp = self.client.post(
            f"{API_PREFIX}/search/{self.config.league}",
            json=query,
        )
        self._search_rl.update_from_headers(resp.headers)
        if resp.status_code == 429:
            retry = int(resp.headers.get("Retry-After", "60"))
            time.sleep(retry)
            return self.search(query)
        resp.raise_for_status()
        data = resp.json()
        return data["id"], data["result"], data["total"]

    def fetch(self, item_ids: list[str], query_id: str) -> list[dict]:
        """Fetch full item data in batches of 10."""
        results: list[dict] = []
        for i in range(0, len(item_ids), 10):
            batch = item_ids[i : i + 10]
            self._fetch_rl.wait()
            ids_str = ",".join(batch)
            resp = self.client.get(
                f"{API_PREFIX}/fetch/{ids_str}",
                params={"query": query_id},
            )
            self._fetch_rl.update_from_headers(resp.headers)
            if resp.status_code == 429:
                retry = int(resp.headers.get("Retry-After", "60"))
                time.sleep(retry)
                resp = self.client.get(
                    f"{API_PREFIX}/fetch/{ids_str}",
                    params={"query": query_id},
                )
                resp.raise_for_status()
            else:
                resp.raise_for_status()
            results.extend(resp.json()["result"])
        return results

    def search_and_fetch(
        self, query: dict, max_items: int | None = None
    ) -> list[dict]:
        if max_items is None:
            max_items = self.config.max_fetch_items
        query_id, result_ids, _total = self.search(query)
        fetch_ids = result_ids[:max_items]
        if not fetch_ids:
            return []
        return self.fetch(fetch_ids, query_id)


# -- query builders --


def build_search_query(
    category: str | None = None,
    stat_filters: list[StatFilter] | None = None,
    min_price: float | None = None,
    max_price: float | None = None,
    item_type: str | None = None,
    rarity: str | None = None,
    online_only: bool = True,
) -> dict:
    query: dict = {
        "query": {
            "status": {"option": "online" if online_only else "any"},
            "filters": {},
        },
        "sort": {"price": "asc"},
    }

    # type / category
    type_filters: dict = {}
    if category:
        type_filters["category"] = {"option": category}
    if rarity:
        type_filters["rarity"] = {"option": rarity}
    if type_filters:
        query["query"]["filters"]["type_filters"] = {"filters": type_filters}

    if item_type:
        query["query"]["type"] = item_type

    # price
    price_filter: dict = {}
    if min_price is not None:
        price_filter["min"] = min_price
    if max_price is not None:
        price_filter["max"] = max_price
    if price_filter:
        price_filter["option"] = "chaos"
        query["query"]["filters"]["trade_filters"] = {
            "filters": {"price": price_filter}
        }

    # stat filters
    if stat_filters:
        group = {
            "type": "count",
            "value": {"min": 1},
            "filters": [
                _stat_filter_to_dict(sf) for sf in stat_filters
            ],
        }
        query["query"]["stats"] = [group]

    return query


def _stat_filter_to_dict(sf: StatFilter) -> dict:
    d: dict = {"id": sf.stat_id, "disabled": False}
    val: dict = {}
    if sf.min_value is not None:
        val["min"] = sf.min_value
    if sf.max_value is not None:
        val["max"] = sf.max_value
    if val:
        d["value"] = val
    return d


# -- stat lookup --


def find_stats(query: str, stats_data: list[dict]) -> list[tuple[str, str]]:
    """Search available stats by text. Returns [(stat_id, display_text), ...]."""
    q = query.lower()
    matches: list[tuple[str, str]] = []
    for category in stats_data:
        for entry in category.get("entries", []):
            if q in entry["text"].lower():
                matches.append((entry["id"], entry["text"]))
    return matches


def resolve_stat(
    query: str, stats_data: list[dict], prefer_pseudo: bool = True
) -> tuple[str, str] | None:
    """Resolve a user-friendly stat name to a (stat_id, text) pair.

    Prefers pseudo stats over explicit when ambiguous, since pseudo stats
    aggregate across mod sources.
    """
    matches = find_stats(query, stats_data)
    if not matches:
        return None
    if len(matches) == 1:
        return matches[0]

    # Prefer exact-ish match
    exact = [m for m in matches if m[1].lower().strip("# +-%.") == query.lower()]
    if len(exact) == 1:
        return exact[0]

    if prefer_pseudo:
        pseudo = [m for m in matches if m[0].startswith("pseudo.")]
        if pseudo:
            return pseudo[0]

    return matches[0]


# -- response parsing --


def parse_listing(raw: dict) -> Listing:
    """Parse a raw fetch-response item into a Listing."""
    item = raw.get("item", {})
    listing_data = raw.get("listing", {})
    price_data = listing_data.get("price", {})

    price = float(price_data.get("amount", 0))
    currency = price_data.get("currency", "unknown")

    stats = _parse_item_stats(item)

    return Listing(
        item_id=raw.get("id", ""),
        price=price,
        currency=currency,
        stats=stats,
        name=item.get("name", ""),
        base_type=item.get("typeLine", ""),
        ilvl=item.get("ilvl", 0),
        listed_at=listing_data.get("indexed", ""),
        account_name=listing_data.get("account", {}).get("name", ""),
        whisper=listing_data.get("whisper", ""),
        icon=item.get("icon", ""),
    )


def _parse_item_stats(item: dict) -> list[StatValue]:
    """Extract structured stat values from an item's extended data."""
    stats: list[StatValue] = []
    extended = item.get("extended", {})
    hashes = extended.get("hashes", {})

    mod_type_keys = {
        "explicit": "explicitMods",
        "implicit": "implicitMods",
        "crafted": "craftedMods",
        "fractured": "fracturedMods",
        "enchant": "enchantMods",
    }

    for mod_type, mod_key in mod_type_keys.items():
        mod_hashes = hashes.get(mod_type, [])
        mod_texts = item.get(mod_key, [])

        for i, hash_entry in enumerate(mod_hashes):
            if not isinstance(hash_entry, list) or not hash_entry:
                continue
            stat_id = hash_entry[0]
            if i >= len(mod_texts):
                continue
            text = mod_texts[i]
            value = extract_number(text)
            if value is not None:
                stats.append(
                    StatValue(
                        stat_id=stat_id,
                        text=text,
                        value=value,
                    )
                )
    return stats


def extract_number(text: str) -> float | None:
    """Extract the primary numerical value from a mod string.

    Handles ranges like "(10-20)" by averaging, and plain numbers like "+95".
    """
    # Ranges: "(10-20) to (30-40)" → average of first range
    range_match = re.search(
        r"\((\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)\)", text
    )
    if range_match:
        lo = float(range_match.group(1))
        hi = float(range_match.group(2))
        return (lo + hi) / 2.0

    match = re.search(r"[+-]?\d+(?:\.\d+)?", text)
    if match:
        return float(match.group())
    return None
