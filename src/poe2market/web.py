from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .analyzer import fit_price_model
from .api import (
    TradeAPI,
    build_search_query,
    find_stats,
    is_offline,
    parse_listing,
    resolve_stat,
)
from .cache import Cache
from .config import Config
from .history import HistoryStore, summarize
from .models import StatFilter
from .observations import ObservationStore
from .scorer import normalize_price, score_listings

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="poe2market", description="Weighted trade search for PoE2")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.middleware("http")
async def _no_cache_assets(request, call_next):
    """Tell the browser never to cache the UI assets, so code changes show up on
    a normal refresh instead of silently running a stale app.js/index.html."""
    resp = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static"):
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp


# ── pages ──────────────────────────────────────────────────────────────


@app.get("/", response_class=HTMLResponse)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


# ── config ─────────────────────────────────────────────────────────────


@app.get("/api/config")
def get_config() -> dict:
    cfg = Config.load()
    return {
        "poesessid_set": bool(cfg.poesessid),
        "poesessid_preview": cfg.poesessid[:8] + "..." if cfg.poesessid else "",
        "league": cfg.league,
        "cache_ttl_hours": cfg.cache_ttl_hours,
        "max_fetch_items": cfg.max_fetch_items,
        "auto_sync_minutes": cfg.auto_sync_minutes,
        "anthropic_key_set": bool(cfg.anthropic_api_key),
        "anthropic_model": cfg.anthropic_model,
    }


class ConfigUpdate(BaseModel):
    poesessid: str | None = None
    league: str | None = None
    max_fetch_items: int | None = None
    auto_sync_minutes: int | None = None
    anthropic_api_key: str | None = None
    anthropic_model: str | None = None


@app.post("/api/config")
def update_config(update: ConfigUpdate) -> dict:
    cfg = Config.load()
    if update.poesessid is not None:
        cfg.poesessid = update.poesessid
    if update.league is not None:
        cfg.league = update.league
    if update.max_fetch_items is not None:
        cfg.max_fetch_items = update.max_fetch_items
    if update.auto_sync_minutes is not None:
        cfg.auto_sync_minutes = max(0, update.auto_sync_minutes)
    if update.anthropic_api_key is not None:
        cfg.anthropic_api_key = update.anthropic_api_key
    if update.anthropic_model is not None:
        cfg.anthropic_model = update.anthropic_model
    cfg.save()
    return {"status": "saved"}


# ── cache management ───────────────────────────────────────────────────


@app.post("/api/cache/clear")
def clear_cache() -> dict:
    cfg = Config.load()
    cache = Cache(cfg.cache_dir, cfg.cache_ttl_hours)
    cache.clear()
    return {"status": "cleared"}


# ── reference data ─────────────────────────────────────────────────────


@app.get("/api/leagues")
def get_leagues() -> list:
    cfg = Config.load()
    cache = Cache(cfg.cache_dir, cfg.cache_ttl_hours)
    cached = cache.get("leagues")
    if cached is not None:
        return cached
    with TradeAPI(cfg) as tapi:
        leagues = tapi.get_leagues()
    cache.set("leagues", leagues)
    return leagues


@app.get("/api/categories")
def get_categories() -> list[dict]:
    """Return category filter options pulled from the trade filters endpoint."""
    cfg = Config.load()
    cache = Cache(cfg.cache_dir, cfg.cache_ttl_hours)

    cached = cache.get("filter_categories")
    if cached is not None:
        return cached

    with TradeAPI(cfg) as tapi:
        resp = tapi.client.get("/api/trade2/data/filters")
        resp.raise_for_status()
        filters = resp.json().get("result", [])

    category_options: list[dict] = []
    for group in filters:
        if group.get("id") != "type_filters":
            continue
        for f in group.get("filters", []):
            if f.get("id") != "category":
                continue
            for opt in f.get("option", {}).get("options", []):
                value = opt.get("id")
                text = opt.get("text", "")
                if value is None or value == "":
                    continue  # skip "Any" entry
                # Group by top-level prefix for dropdown optgroup
                top = value.split(".", 1)[0] if "." in value else value
                category_options.append({
                    "value": value,
                    "label": text,
                    "group": top.capitalize(),
                })

    cache.set("filter_categories", category_options)
    return category_options


@app.get("/api/stats")
def search_stats_endpoint(q: str = "", limit: int = 30) -> list[dict]:
    cfg = Config.load()
    cache = Cache(cfg.cache_dir, cfg.cache_ttl_hours)

    cached = cache.get("stats_data")
    if cached is not None:
        stats_data = cached
    else:
        with TradeAPI(cfg) as tapi:
            stats_data = tapi.get_stats()
        cache.set("stats_data", stats_data)

    if not q or len(q) < 2:
        return []

    matches = find_stats(q, stats_data)
    return [{"id": m[0], "text": m[1]} for m in matches[:limit]]


# ── analyze ────────────────────────────────────────────────────────────


class AnalyzeRequest(BaseModel):
    category: str
    attributes: list[str] | None = None  # subset of str/dex/int base attributes
    min_divine: float = 1.0  # floor of the sampled price range, in divine
    max_items: int | None = None
    min_occurrence: float = 0.1
    use_history: bool = True  # include accumulated saved observations


def _analyze_bands(min_divine: float):
    """Price bands (in DIVINE) for the regression sample, floored at
    ``min_divine`` and fanning out so higher-value items are represented.
    The price filter uses the "divine" option, so bands must be in divine.
    """
    floor = max(0.01, min_divine)
    mults = [1, 2, 4, 8, 20, 60]
    edges = [floor * m for m in mults]
    bands = [(None, floor)]  # everything below the floor in one band
    for i, lo in enumerate(edges):
        hi = edges[i + 1] if i + 1 < len(edges) else None
        bands.append((lo, hi))
    return bands


@app.post("/api/analyze")
def analyze_endpoint(req: AnalyzeRequest) -> dict:
    cfg = Config.load()
    if not cfg.poesessid:
        raise HTTPException(401, "POESESSID not configured")

    rates = _exalted_rates(cfg)
    divine_price = rates.get("divine", 1.0) or 1.0
    bands = _analyze_bands(req.min_divine)

    # Sample across price bands so the regression sees real price variance — a
    # single price-sorted search only returns near-identical cheapest listings.
    # search_and_fetch_price_diverse returns OFFLINE listings only.
    with TradeAPI(cfg) as tapi:
        raw_items = tapi.search_and_fetch_price_diverse(
            req.category,
            max_items=req.max_items,
            bands=bands,
            attributes=req.attributes,
        )

    # Save every fetched item so the sample accumulates across searches.
    import time as _time

    obs = ObservationStore(cfg.cache_dir)
    obs.record(cfg.league, req.category, raw_items, _time.time())

    fresh = [parse_listing(r) for r in raw_items]
    if req.use_history:
        # Regress over the full accumulated set for this category/attributes
        # (includes the items we just fetched), not just this one search.
        listings = obs.query(cfg.league, req.category, req.attributes)
    else:
        listings = fresh
    saved_count = obs.count(cfg.league, req.category)

    if not listings:
        return {
            "listings_count": 0,
            "coefficients": [],
            "note": "No listings found for this category.",
        }

    prices = [normalize_price(l.price, l.currency, rates) for l in listings]
    distinct = len({round(p, 1) for p in prices})
    note = None
    if distinct < 3:
        note = (
            "Not enough price variation in the sample to fit a reliable model — "
            "this category may have too few or too uniformly-priced listings."
        )

    coefficients = fit_price_model(
        listings, min_occurrence=req.min_occurrence, chaos_rates=rates
    )

    return {
        "listings_count": len(listings),
        "fetched_now": len(fresh),
        "saved_total": saved_count,
        "used_history": req.use_history,
        "price_min": round(min(prices), 1) if prices else 0,
        "price_max": round(max(prices), 1) if prices else 0,
        "note": note,
        "coefficients": [
            {
                "stat_id": c.stat_id,
                "text": c.text,
                "coefficient": c.coefficient,
                "std_error": c.std_error,
                "sample_count": c.sample_count,
            }
            for c in coefficients
        ],
    }


# ── deals ──────────────────────────────────────────────────────────────


class StatWeight(BaseModel):
    stat_id: str
    text: str = ""
    weight: float = 1.0
    min_value: float | None = None


class DealsRequest(BaseModel):
    category: str
    stats: list[StatWeight]
    attributes: list[str] | None = None  # subset of str/dex/int base attributes
    min_price: float | None = None  # divine
    max_price: float | None = None  # divine
    max_items: int | None = None
    limit: int = 20


@app.post("/api/deals")
def deals_endpoint(req: DealsRequest) -> dict:
    cfg = Config.load()
    if not cfg.poesessid:
        raise HTTPException(401, "POESESSID not configured")
    if not req.stats:
        raise HTTPException(400, "At least one stat is required")

    weights = {s.stat_id: s.weight for s in req.stats}
    stat_filters = [
        StatFilter(stat_id=s.stat_id, min_value=s.min_value) for s in req.stats
    ]
    stat_labels = {s.stat_id: s.text or s.stat_id for s in req.stats}

    query = build_search_query(
        category=req.category,
        stat_filters=stat_filters,
        min_price=req.min_price,
        max_price=req.max_price,
        attributes=req.attributes,
    )

    rates = _exalted_rates(cfg)
    divine_price = rates.get("divine", 1.0) or 1.0

    with TradeAPI(cfg) as tapi:
        raw_items = [r for r in tapi.search_and_fetch(query, req.max_items) if is_offline(r)]

    # Save fetched items so they feed future analysis.
    import time as _time

    ObservationStore(cfg.cache_dir).record(
        cfg.league, req.category, raw_items, _time.time()
    )

    listings = [parse_listing(r) for r in raw_items]
    ranked = score_listings(listings, weights, chaos_rates=rates)

    # Use actual mod text from items when available, fall back to stat filter label
    for listing in listings:
        for sv in listing.stats:
            if sv.stat_id in weights and sv.stat_id not in stat_labels:
                stat_labels[sv.stat_id] = sv.text

    return {
        "listings_count": len(listings),
        "divine_price": divine_price,
        "deals": [
            {
                "item_id": d.listing.item_id,
                "name": d.listing.name,
                "base_type": d.listing.base_type,
                "ilvl": d.listing.ilvl,
                "price": d.listing.price,
                "currency": d.listing.currency,
                "exalted_price": normalize_price(
                    d.listing.price, d.listing.currency, rates
                ),
                "divine_price_eq": normalize_price(
                    d.listing.price, d.listing.currency, rates
                ) / divine_price,
                "weighted_score": d.weighted_score,
                "value_ratio": d.value_ratio,
                "account": d.listing.account_name,
                "whisper": d.listing.whisper,
                "icon": d.listing.icon,
                "contributions": [
                    {
                        "stat_id": sid,
                        "text": stat_labels.get(sid, sid),
                        "value": contrib,
                    }
                    for sid, contrib in sorted(
                        d.stat_contributions.items(), key=lambda x: -x[1]
                    )
                ],
                "all_stats": [
                    {"text": sv.text, "value": sv.value}
                    for sv in d.listing.stats
                ],
            }
            for d in ranked[: req.limit]
        ],
    }


# ── merchant history ───────────────────────────────────────────────────


def _exalted_rates(cfg) -> dict:
    """Live poe2scout exalted rates for the league, cached to avoid refetching.

    Falls back to the static table inside fetch_exalted_rates on any failure.
    """
    cache = Cache(cfg.cache_dir, cfg.cache_ttl_hours)
    cached = cache.get("exalted_rates")
    if cached is not None:
        return cached
    from .stash import fetch_exalted_rates

    rates, _divine = fetch_exalted_rates(league=cfg.league)
    cache.set("exalted_rates", rates)
    return rates


def _sale_to_dict(sale, rates) -> dict:
    return {
        "sale_id": sale.sale_id,
        "time": sale.time,
        "amount": sale.amount,
        "currency": sale.currency,
        "exalted_value": round(sale.exalted_value(rates), 2),
        "name": sale.name,
        "base_type": sale.base_type,
        "rarity": sale.rarity,
        "ilvl": sale.ilvl,
        "stack_size": sale.stack_size,
        "icon": sale.icon,
        "implicit_mods": sale.implicit_mods,
        "explicit_mods": sale.explicit_mods,
        "rune_mods": sale.rune_mods,
        "enchant_mods": sale.enchant_mods,
        "fractured_mods": sale.fractured_mods,
        "properties": sale.properties,
        "requirements": sale.requirements,
        "has_detail": bool(
            sale.explicit_mods
            or sale.implicit_mods
            or sale.rune_mods
            or sale.enchant_mods
            or sale.fractured_mods
        ),
    }


# Last sync outcome, shared between the manual endpoint and the auto-sync loop
# so the UI can show when data last refreshed and whether it succeeded.
_sync_status: dict = {
    "last_attempt": None,   # epoch seconds
    "last_success": None,   # epoch seconds
    "last_new": 0,
    "total": 0,
    "error": None,
    "auto": False,          # whether the background loop is running
}


def _perform_sync(cfg) -> dict:
    """Fetch the latest sales and merge them into the store. Raises on failure."""
    store = HistoryStore(cfg.cache_dir)
    with TradeAPI(cfg) as tapi:
        raw_entries = tapi.get_history(cfg.league)
    new_count = store.upsert_many(cfg.league, raw_entries)
    return {
        "league": cfg.league,
        "fetched": len(raw_entries),
        "new": new_count,
        "total": store.count(cfg.league),
    }


@app.post("/api/history/sync")
def sync_history() -> dict:
    """Pull the latest sales from the trade API and merge into the local store.

    Returns how many new sales were added and the running total for the league.
    """
    import time

    cfg = Config.load()
    if not cfg.poesessid:
        raise HTTPException(401, "POESESSID not configured")

    _sync_status["last_attempt"] = time.time()
    try:
        result = _perform_sync(cfg)
    except Exception as e:  # surface API/auth/Cloudflare failures to the UI
        import httpx

        from .api import RateLimited

        if isinstance(e, RateLimited):
            mins = max(1, round(e.retry_after / 60))
            _sync_status["error"] = "ratelimit"
            raise HTTPException(
                429,
                f"Trade history is rate-limited (15 syncs per 3 hours). Try again "
                f"in ~{mins} min — the dashboard auto-syncs in the background anyway.",
            ) from None
        if isinstance(e, httpx.HTTPStatusError):
            code = e.response.status_code
            if code in (401, 403):
                _sync_status["error"] = "auth"
                raise HTTPException(
                    401,
                    "Trade API rejected the request (401/403). Your POESESSID "
                    "may be expired — refresh it in Settings.",
                ) from None
            if code == 404:
                raise HTTPException(
                    404,
                    f"No history for league '{cfg.league}'. Check the league "
                    "name in Settings.",
                ) from None
            raise HTTPException(502, f"Trade API error {code}.") from None
        raise HTTPException(502, f"Could not reach trade API: {e}") from None

    _sync_status.update(
        last_success=time.time(),
        last_new=result["new"],
        total=result["total"],
        error=None,
    )
    return result


@app.get("/api/history/status")
def history_status() -> dict:
    """Sync status (last attempt/success, totals) for the auto-sync indicator."""
    cfg = Config.load()
    return {
        **_sync_status,
        "auto_sync_minutes": cfg.auto_sync_minutes,
        "poesessid_set": bool(cfg.poesessid),
    }


@app.get("/api/history")
def get_history_list() -> dict:
    """Return all locally stored sales for the active league (newest first)."""
    cfg = Config.load()
    store = HistoryStore(cfg.cache_dir)
    sales = store.all(cfg.league)
    rates = _exalted_rates(cfg)

    return {
        "league": cfg.league,
        "count": len(sales),
        "sales": [_sale_to_dict(s, rates) for s in sales],
    }


@app.get("/api/history/summary")
def get_history_summary() -> dict:
    """Return dashboard aggregates for the active league."""
    cfg = Config.load()
    store = HistoryStore(cfg.cache_dir)
    sales = store.all(cfg.league)
    summary = summarize(sales, _exalted_rates(cfg))
    return summary.to_dict()


@app.post("/api/history/clear")
def clear_history() -> dict:
    cfg = Config.load()
    store = HistoryStore(cfg.cache_dir)
    store.clear(cfg.league)
    return {"status": "cleared", "league": cfg.league}


# ── boots sale-tracker ─────────────────────────────────────────────────


@app.get("/api/tracker")
def get_tracker() -> dict:
    """Boots sale-tracker report for the Tracker tab: inferred clearing prices
    per mod-bucket (calibrated against real sales), plus stale-listing ceilings.
    Read-only — just queries tracker.db; prices are returned in divine."""
    import time

    from . import tracker as tk

    cfg = Config.load()
    rates = _exalted_rates(cfg)
    div = rates.get("divine", 1.0) or 1.0
    store = tk.TrackerStore(cfg.cache_dir)
    cal = tk.calibrate(cfg)
    cf = cal["global_factor"]

    def to_d(ex: float) -> float:
        return round(ex / div, 2)

    buckets = [
        {
            "mod_sig": r["mod_sig"],
            "n_exits": r["n_exits"],
            "exit_median_d": to_d(r["median_exit_ex"]),
            "est_clear_d": to_d(r["median_exit_ex"] * cf),
            "p25_d": to_d(r["p25_exit_ex"]),
            "p75_d": to_d(r["p75_exit_ex"]),
            "days_on_market": r["median_days_on_market"],
            "reprice_down_pct": round(r["reprice_down_rate"] * 100),
        }
        for r in store.clearing_report(cfg.league)
    ]
    ceilings = [
        {
            "mod_sig": c["mod_sig"], "price_d": to_d(c["price_ex"]),
            "age_days": c["age_days"], "tier": c["tier"], "reprice_downs": c["reprice_downs"],
        }
        for c in store.stale_ceilings(cfg.league)
    ]

    now = time.time()
    items = []
    for it in store.all_listings(cfg.league):
        if it["defc"] not in tk.FOCUS_DEFENCE:
            continue  # ES / EV / EV+ES only — ignore armour
        age_d = round((now - it["first_seen"]) / 86400.0, 2)
        items.append({
            "base": it["base"], "defc": it["defc"], "sockets": it["sockets"],
            "ms": it["ms"], "ms_explicit": it["ms_explicit"],
            "ms_rune": it["ms"] > it["ms_explicit"], "chaos_res": it["chaos_res"],
            "life": it["life"], "res": it["res"], "rarity": it["rarity"],
            "corrupted": bool(it["corrupted"]), "runeforged": bool(it["runeforged"]),
            "price_d": to_d(it["last_price_ex"]),
            "min_d": to_d(it["min_price_ex"]), "max_d": to_d(it["max_price_ex"]),
            "reprice_up": it["n_reprice_up"], "reprice_down": it["n_reprice_down"],
            "sightings": it["n_sightings"], "status": it["status"],
            "age_days": age_d,
            "exit_d": to_d(it["exit_price_ex"]) if it["exit_price_ex"] is not None else None,
        })
    return {
        "league": cfg.league,
        "divine_price": round(div),
        "interval_min": cfg.tracker_minutes,
        "summary": store.summary(cfg.league),
        "last_poll_ts": store.last_poll_ts(cfg.league),
        "status": _tracker_status,
        "calibration": {
            "factor": round(cf, 2), "n_matched": cal["n_matched"],
            "n_sold_buckets": cal["n_sold_buckets"], "n_exit_buckets": cal["n_exit_buckets"],
        },
        "buckets": buckets,
        "ceilings": ceilings,
        "items": items,
    }


# ── saved item observations ────────────────────────────────────────────


@app.get("/api/observations")
def observations_stats() -> dict:
    """Counts of saved items, total and per category, for the active league."""
    cfg = Config.load()
    obs = ObservationStore(cfg.cache_dir)
    return {
        "league": cfg.league,
        "total": obs.count(cfg.league),
        "by_category": obs.category_counts(cfg.league),
    }


@app.post("/api/observations/clear")
def clear_observations() -> dict:
    cfg = Config.load()
    ObservationStore(cfg.cache_dir).clear(cfg.league)
    return {"status": "cleared", "league": cfg.league}


# ── prompt interpretation ──────────────────────────────────────────────


class InterpretRequest(BaseModel):
    prompt: str


class _InterpretedStat(BaseModel):
    query: str
    weight: float = 1.0
    min_value: float | None = None


class _InterpretedSearch(BaseModel):
    category: str
    stats: list[_InterpretedStat]
    min_price: float | None = None
    max_price: float | None = None
    limit: int = 20
    explanation: str


INTERPRET_SYSTEM = """You are a Path of Exile 2 trade search assistant. Convert \
the user's plain-English item description into structured search parameters for \
the poe2market tool, which ranks items by weighted stat value per exalted orb.

Rules:
- `category` MUST be one of the valid category IDs listed below. Pick the most \
specific one that matches the user's intent.
- `stats` is a list of desired stats:
    - `query` is a human-readable stat name ("maximum life", "fire resistance", \
"spell damage"). The tool fuzzy-matches this to the actual PoE2 stat ID, so use \
the natural phrasing that appears on items.
    - `weight` is relative importance (1.0 baseline; 3.0 means "three times as \
important as weight 1"). Infer from emphasis in the prompt.
    - `min_value` is optional; only set when the user explicitly specifies a \
minimum.
- `min_price` / `max_price` in divine orbs; only set when specified.
- `limit` defaults to 20.
- `explanation` is a one-sentence summary of your interpretation.

Valid categories:
{categories}

Common PoE2 stat names to use for queries (prefer pseudo-aggregated where it \
makes sense):
  maximum life, maximum mana, fire resistance, cold resistance, lightning \
resistance, chaos resistance, spell damage, attack speed, cast speed, critical \
strike chance, critical damage, armour, evasion rating, energy shield, \
movement speed, rarity, rune sockets, spirit, accuracy rating, life regeneration
"""


@app.post("/api/interpret")
def interpret_endpoint(req: InterpretRequest) -> dict:
    cfg = Config.load()
    if not cfg.anthropic_api_key:
        raise HTTPException(
            401,
            "Anthropic API key not configured. Set it in Settings or the "
            "ANTHROPIC_API_KEY env var.",
        )

    try:
        import anthropic
    except ImportError:
        raise HTTPException(500, "anthropic package not installed") from None

    # Build the category context
    categories = get_categories()
    valid_values = {c["value"] for c in categories}
    cat_text = "\n".join(f"  {c['value']}: {c['label']}" for c in categories)
    system = INTERPRET_SYSTEM.format(categories=cat_text)

    client = anthropic.Anthropic(api_key=cfg.anthropic_api_key)

    try:
        response = client.messages.parse(
            model=cfg.anthropic_model,
            max_tokens=2048,
            system=system,
            messages=[{"role": "user", "content": req.prompt}],
            output_format=_InterpretedSearch,
        )
    except anthropic.AuthenticationError:
        raise HTTPException(401, "Invalid Anthropic API key") from None
    except anthropic.RateLimitError:
        raise HTTPException(429, "Anthropic rate limit hit; try again in a moment") from None
    except anthropic.APIStatusError as e:
        raise HTTPException(502, f"Anthropic API error: {e.message}") from None

    parsed: _InterpretedSearch = response.parsed_output

    if parsed.category not in valid_values:
        raise HTTPException(
            400,
            f"Model returned unknown category '{parsed.category}'. "
            "Try a more specific prompt or pick a category manually.",
        )

    # Resolve stat queries → stat IDs via cached stats data
    cache = Cache(cfg.cache_dir, cfg.cache_ttl_hours)
    stats_data = cache.get("stats_data")
    if stats_data is None:
        with TradeAPI(cfg) as tapi:
            stats_data = tapi.get_stats()
        cache.set("stats_data", stats_data)

    resolved: list[dict] = []
    unresolved: list[str] = []
    for s in parsed.stats:
        result = resolve_stat(s.query, stats_data)
        if result is None:
            unresolved.append(s.query)
            continue
        stat_id, text = result
        resolved.append(
            {
                "stat_id": stat_id,
                "text": text,
                "weight": s.weight,
                "min_value": s.min_value,
                "original_query": s.query,
            }
        )

    return {
        "category": parsed.category,
        "stats": resolved,
        "unresolved_stats": unresolved,
        "min_price": parsed.min_price,
        "max_price": parsed.max_price,
        "limit": parsed.limit,
        "explanation": parsed.explanation,
    }


# ── background auto-sync ───────────────────────────────────────────────

_autosync_started = False


def _autosync_loop() -> None:
    """Periodically sync sale history so the dashboard stays current without the
    user clicking. One request per interval keeps API load negligible; the
    interval and on/off come from config (re-read each cycle so Settings changes
    take effect without a restart)."""
    import time

    # Small initial delay so startup isn't blocked and the session is ready.
    time.sleep(15)
    while True:
        cfg = Config.load()
        interval = max(0, cfg.auto_sync_minutes)
        if interval == 0:
            time.sleep(60)  # disabled — re-check in a minute in case it's enabled
            continue
        if cfg.poesessid:
            _sync_status["last_attempt"] = time.time()
            try:
                result = _perform_sync(cfg)
                _sync_status.update(
                    last_success=time.time(),
                    last_new=result["new"],
                    total=result["total"],
                    error=None,
                )
            except Exception as e:  # never let the loop die
                import httpx

                from .api import RateLimited

                if isinstance(e, RateLimited):
                    _sync_status["error"] = "ratelimit"  # transient; next cycle retries
                elif isinstance(e, httpx.HTTPStatusError) and e.response.status_code in (401, 403):
                    _sync_status["error"] = "auth"
                else:
                    _sync_status["error"] = "network"
        time.sleep(interval * 60)


@app.on_event("startup")
def _start_autosync() -> None:
    global _autosync_started
    if _autosync_started:
        return
    _autosync_started = True
    _sync_status["auto"] = True
    import threading

    threading.Thread(target=_autosync_loop, daemon=True, name="autosync").start()


# ── background sale-tracker ────────────────────────────────────────────

_tracker_started = False
_tracker_status: dict = {"last_success": None, "offline": 0, "new": 0, "gone": 0, "error": None}


def _tracker_loop() -> None:
    """Periodically poll the boots market (OFFLINE-only) to track listing
    lifecycles and infer real clearing prices. Interval from config
    (``tracker_minutes``; 0 disables), re-read each cycle so Settings changes
    take effect without a restart. Staggered after auto-sync so the two
    background jobs don't burst the trade API at the same instant."""
    import time

    from . import tracker

    time.sleep(25)
    while True:
        cfg = Config.load()
        interval = max(0, cfg.tracker_minutes)
        if interval == 0:
            time.sleep(60)  # disabled — re-check in case it's re-enabled
            continue
        if cfg.poesessid and not tracker.recently_polled(cfg, interval):
            _tracker_status["last_attempt"] = time.time()
            try:
                res = tracker.run_poll(cfg)
                _tracker_status.update(last_success=time.time(), offline=res.offline,
                                       new=res.new, gone=res.marked_gone, error=None)
            except Exception as e:  # never let the loop die
                import httpx

                if isinstance(e, httpx.HTTPStatusError) and e.response.status_code in (401, 403):
                    _tracker_status["error"] = "auth"
                else:
                    _tracker_status["error"] = "network"
        time.sleep(interval * 60)


@app.on_event("startup")
def _start_tracker() -> None:
    global _tracker_started
    if _tracker_started:
        return
    _tracker_started = True
    import threading

    cfg = Config.load()
    print(f"[tracker] background sale-tracker started (every {cfg.tracker_minutes}m, "
          f"offline-only)", flush=True)
    threading.Thread(target=_tracker_loop, daemon=True, name="tracker").start()


def run(host: str = "127.0.0.1", port: int = 8000, reload: bool = False) -> None:
    """Start the web UI server."""
    import uvicorn
    uvicorn.run(
        "poe2market.web:app" if reload else app,
        host=host,
        port=port,
        reload=reload,
    )
