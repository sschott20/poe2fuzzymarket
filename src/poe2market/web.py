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
    parse_listing,
    resolve_stat,
)
from .cache import Cache
from .config import Config
from .models import StatFilter
from .scorer import normalize_price, score_listings

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="poe2market", description="Weighted trade search for PoE2")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


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
        "anthropic_key_set": bool(cfg.anthropic_api_key),
        "anthropic_model": cfg.anthropic_model,
    }


class ConfigUpdate(BaseModel):
    poesessid: str | None = None
    league: str | None = None
    max_fetch_items: int | None = None
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
    min_price: float | None = None
    max_price: float | None = None
    max_items: int | None = None
    min_occurrence: float = 0.1
    online_only: bool = True


@app.post("/api/analyze")
def analyze_endpoint(req: AnalyzeRequest) -> dict:
    cfg = Config.load()
    if not cfg.poesessid:
        raise HTTPException(401, "POESESSID not configured")

    query = build_search_query(
        category=req.category,
        min_price=req.min_price,
        max_price=req.max_price,
        online_only=req.online_only,
    )

    with TradeAPI(cfg) as tapi:
        raw_items = tapi.search_and_fetch(query, req.max_items)

    listings = [parse_listing(r) for r in raw_items]
    if not listings:
        return {"listings_count": 0, "coefficients": []}

    coefficients = fit_price_model(listings, min_occurrence=req.min_occurrence)

    return {
        "listings_count": len(listings),
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
    min_price: float | None = None
    max_price: float | None = None
    max_items: int | None = None
    limit: int = 20
    online_only: bool = True


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
        online_only=req.online_only,
    )

    with TradeAPI(cfg) as tapi:
        raw_items = tapi.search_and_fetch(query, req.max_items)

    listings = [parse_listing(r) for r in raw_items]
    ranked = score_listings(listings, weights)

    # Use actual mod text from items when available, fall back to stat filter label
    for listing in listings:
        for sv in listing.stats:
            if sv.stat_id in weights and sv.stat_id not in stat_labels:
                stat_labels[sv.stat_id] = sv.text

    return {
        "listings_count": len(listings),
        "deals": [
            {
                "item_id": d.listing.item_id,
                "name": d.listing.name,
                "base_type": d.listing.base_type,
                "ilvl": d.listing.ilvl,
                "price": d.listing.price,
                "currency": d.listing.currency,
                "chaos_price": normalize_price(
                    d.listing.price, d.listing.currency
                ),
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
the poe2market tool, which ranks items by weighted stat value per chaos orb.

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
- `min_price` / `max_price` in chaos orbs; only set when specified.
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


def run(host: str = "127.0.0.1", port: int = 8000, reload: bool = False) -> None:
    """Start the web UI server."""
    import uvicorn
    uvicorn.run(
        "poe2market.web:app" if reload else app,
        host=host,
        port=port,
        reload=reload,
    )
