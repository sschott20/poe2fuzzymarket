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
    }


class ConfigUpdate(BaseModel):
    poesessid: str | None = None
    league: str | None = None
    max_fetch_items: int | None = None


@app.post("/api/config")
def update_config(update: ConfigUpdate) -> dict:
    cfg = Config.load()
    if update.poesessid is not None:
        cfg.poesessid = update.poesessid
    if update.league is not None:
        cfg.league = update.league
    if update.max_fetch_items is not None:
        cfg.max_fetch_items = update.max_fetch_items
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
    """Return flattened category options suitable for a dropdown."""
    cfg = Config.load()
    cache = Cache(cfg.cache_dir, cfg.cache_ttl_hours)

    cached = cache.get("items_data")
    if cached is not None:
        items_data = cached
    else:
        with TradeAPI(cfg) as tapi:
            items_data = tapi.get_items()
        cache.set("items_data", items_data)

    # Build flat list: [{value: "armour.body", label: "Body Armour", group: "Armour"}, ...]
    options: list[dict] = []
    for group in items_data:
        group_id = group.get("id", "")
        group_label = group.get("label", group_id)
        for entry in group.get("entries", []):
            value = group_id
            type_name = entry.get("type", "")
            options.append({
                "value": value,
                "label": type_name,
                "group": group_label,
            })

    # De-dup by value keeping first occurrence
    seen: set[str] = set()
    unique_options: list[dict] = []
    for opt in options:
        if opt["value"] not in seen:
            seen.add(opt["value"])
            unique_options.append(opt)
    return unique_options


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


def run(host: str = "127.0.0.1", port: int = 8000, reload: bool = False) -> None:
    """Start the web UI server."""
    import uvicorn
    uvicorn.run(
        "poe2market.web:app" if reload else app,
        host=host,
        port=port,
        reload=reload,
    )
