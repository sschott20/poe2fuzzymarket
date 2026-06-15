import re
import threading
import time

import httpx

from .config import Config
from .models import Listing, StatFilter, StatValue

BASE_URL = "https://www.pathofexile.com"
API_PREFIX = "/api/trade2"

SEARCH_RATE_LIMIT = (5, 12)
FETCH_RATE_LIMIT = (12, 6)
HISTORY_RATE_LIMIT = (5, 60)  # the trade-history endpoint's own strict bucket


class RateLimited(Exception):
    """Raised when an endpoint with a long Retry-After (e.g. trade history,
    15 requests per 3 hours) is rate-limited — caller should surface, not sleep."""

    def __init__(self, retry_after: int):
        self.retry_after = retry_after
        super().__init__(f"rate-limited; retry in ~{retry_after}s")


class RateLimiter:
    """Sliding-window rate limiter that respects API response headers.

    Thread-safe: the background tracker (its own thread) and interactive requests
    (history sync, appraise) share a single limiter per process, so they throttle
    cooperatively instead of each thinking it has the full budget and triggering
    server 429s (which then sleep on Retry-After and stall the foreground)."""

    def __init__(self, max_requests: int, per_seconds: float):
        self.max_requests = max_requests
        self.per_seconds = per_seconds
        self._timestamps: list[float] = []
        self._lock = threading.Lock()

    def wait(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                self._timestamps = [t for t in self._timestamps if now - t < self.per_seconds]
                if len(self._timestamps) < self.max_requests:
                    self._timestamps.append(now)
                    return
                sleep_time = self._timestamps[0] + self.per_seconds - now
            # sleep OUTSIDE the lock so other threads can also acquire a slot
            time.sleep(max(0.0, sleep_time))

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
                        with self._lock:
                            self.max_requests = int(parts[0])
                            self.per_seconds = float(parts[1])
                    except ValueError:
                        pass


class TradeAPI:
    # Shared across ALL instances/threads in the process so the background tracker
    # and interactive requests cooperatively respect one budget (no 429 storms).
    _search_rl = RateLimiter(*SEARCH_RATE_LIMIT)
    _fetch_rl = RateLimiter(*FETCH_RATE_LIMIT)
    _history_rl = RateLimiter(*HISTORY_RATE_LIMIT)

    def __init__(self, config: Config):
        self.config = config
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

    # -- merchant history --

    def get_history(self, league: str | None = None) -> list[dict]:
        """Fetch your recent hideout-merchant sales for a league.

        Hits ``/api/trade2/history/{league}`` with the POESESSID cookie. The
        server returns roughly the last ~100 sales; callers persist + dedupe to
        retain more. Returns the raw entry list (``result`` or ``entries``).
        """
        league = league or self.config.league
        # The history endpoint has its OWN, very strict bucket (≈5/60s and
        # 15 per 3 HOURS) — separate from search/fetch. Use a dedicated limiter
        # so its tight limits don't pollute the shared fetch limiter, and FAIL
        # FAST on 429 (Retry-After can be minutes) instead of sleeping/hanging.
        self._history_rl.wait()
        resp = self.client.get(
            f"{API_PREFIX}/history/{league}",
            headers={"Referer": f"{BASE_URL}/trade2"},
        )
        self._history_rl.update_from_headers(resp.headers)
        if resp.status_code == 429:
            retry = int(resp.headers.get("Retry-After", "60"))
            raise RateLimited(retry)
        resp.raise_for_status()
        data = resp.json()
        return data.get("result") or data.get("entries") or []

    # -- stash / account --

    def get_account_name(self) -> str:
        """Resolve the logged-in account name from the my-account page.

        There is no JSON endpoint for this in the legacy (cookie) world, so we
        scrape the authenticated HTML page for the view-profile link.
        """
        resp = self.client.get(
            f"{BASE_URL}/my-account", headers={"Accept": "text/html"}
        )
        resp.raise_for_status()
        m = re.search(r"/account/view-profile/([^\"/?]+)", resp.text)
        if not m:
            raise ValueError(
                "Could not determine account name — is the POESESSID valid and "
                "logged in?"
            )
        from urllib.parse import unquote

        return unquote(m.group(1))

    def get_stash_tabs(
        self, account_name: str, league: str | None = None, realm: str = "poe2"
    ) -> list[dict]:
        """List stash tab metadata (name, type, index) for an account."""
        league = league or self.config.league
        self._fetch_rl.wait()
        resp = self.client.get(
            f"{BASE_URL}/character-window/get-stash-items",
            params={
                "accountName": account_name,
                "realm": realm,
                "league": league,
                "tabs": 1,
                "tabIndex": 0,
            },
            headers={"Referer": f"{BASE_URL}/"},
        )
        self._fetch_rl.update_from_headers(resp.headers)
        if resp.status_code == 429:
            time.sleep(int(resp.headers.get("Retry-After", "60")))
            return self.get_stash_tabs(account_name, league, realm)
        resp.raise_for_status()
        return resp.json().get("tabs", [])

    def get_stash_tab(
        self,
        account_name: str,
        tab_index: int,
        league: str | None = None,
        realm: str = "poe2",
    ) -> list[dict]:
        """Return the item objects in a single stash tab."""
        league = league or self.config.league
        self._fetch_rl.wait()
        resp = self.client.get(
            f"{BASE_URL}/character-window/get-stash-items",
            params={
                "accountName": account_name,
                "realm": realm,
                "league": league,
                "tabs": 0,
                "tabIndex": tab_index,
            },
            headers={"Referer": f"{BASE_URL}/"},
        )
        self._fetch_rl.update_from_headers(resp.headers)
        if resp.status_code == 429:
            time.sleep(int(resp.headers.get("Retry-After", "60")))
            return self.get_stash_tab(account_name, tab_index, league, realm)
        resp.raise_for_status()
        return resp.json().get("items", [])

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
            ids_str = ",".join(batch)
            while True:  # back off and retry as long as the API returns 429
                self._fetch_rl.wait()
                resp = self.client.get(
                    f"{API_PREFIX}/fetch/{ids_str}",
                    params={"query": query_id},
                )
                self._fetch_rl.update_from_headers(resp.headers)
                if resp.status_code == 429:
                    time.sleep(int(resp.headers.get("Retry-After", "60")))
                    continue
                resp.raise_for_status()
                break
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

    def search_and_fetch_price_diverse(
        self,
        category: str | None,
        max_items: int | None = None,
        bands: list[tuple[float | None, float | None]] | None = None,
        attributes: list[str] | None = None,
    ) -> list[dict]:
        """Fetch a price-diverse OFFLINE sample for regression.

        Each trade search is sorted by price ascending, so a single search only
        returns the cheapest listings — which have almost no price variance,
        making regression meaningless. This runs one search per price band (in
        DIVINE) and combines them, giving the spread the regression needs.
        Online listings are dropped (offline-only policy).
        """
        if max_items is None:
            max_items = self.config.max_fetch_items
        if bands is None:
            # divine-denominated bands (price filter uses the "divine" option)
            bands = [
                (None, 0.5), (0.5, 2), (2, 5),
                (5, 20), (20, 100), (100, None),
            ]
        per_band = max(8, max_items // len(bands))
        seen: set[str] = set()
        combined: list[dict] = []
        for lo, hi in bands:
            query = build_search_query(
                category=category,
                min_price=lo,
                max_price=hi,
                attributes=attributes,
            )
            try:
                raw = self.search_and_fetch(query, per_band)
            except Exception:
                continue  # a sparse/empty band shouldn't abort the whole sample
            for r in raw:
                rid = r.get("id")
                if rid and rid not in seen and is_offline(r):
                    seen.add(rid)
                    combined.append(r)
        return combined


# -- query builders --


# Player attribute -> base defence stat. A base's defences track its attribute
# requirements: Str→Armour, Dex→Evasion, Int→Energy Shield. So "dex/int boots"
# means a base with evasion + energy shield and no armour.
ATTRIBUTE_TO_DEFENCE = {"str": "ar", "dex": "ev", "int": "es"}


def is_offline(raw: dict) -> bool:
    """True if a fetched listing's seller is offline. We always search status
    "any" (the API has no "offline" option) and keep only offline listings —
    online listings are dominated by always-online price-fixers and are excluded
    per project policy."""
    return not ((raw.get("listing") or {}).get("account") or {}).get("online")


def build_search_query(
    category: str | None = None,
    stat_filters: list[StatFilter] | None = None,
    min_price: float | None = None,
    max_price: float | None = None,
    item_type: str | None = None,
    rarity: str | None = None,
    attributes: list[str] | None = None,
) -> dict:
    # Always "any": offline-only is enforced by filtering fetched listings with
    # is_offline(), since the trade API has no "offline" status option.
    query: dict = {
        "query": {
            "status": {"option": "any"},
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

    # base attribute combo (e.g. dex+int -> evasion & energy shield, no armour)
    if attributes:
        attrs = {a.lower() for a in attributes}
        if attrs & set(ATTRIBUTE_TO_DEFENCE):
            eq: dict = {}
            for attr, defence in ATTRIBUTE_TO_DEFENCE.items():
                if attr in attrs:
                    eq[defence] = {"min": 1}
                else:
                    eq[defence] = {"max": 0}
            query["query"]["filters"]["equipment_filters"] = {"filters": eq}

    # price
    price_filter: dict = {}
    if min_price is not None:
        price_filter["min"] = min_price
    if max_price is not None:
        price_filter["max"] = max_price
    if price_filter:
        # MUST be "divine": an "exalted" price filter silently fails to match
        # divine-listed items (how all expensive items are priced). min/max are
        # therefore interpreted in divine.
        price_filter["option"] = "divine"
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
