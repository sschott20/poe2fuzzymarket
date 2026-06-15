import sys

import click
from rich.console import Console
from rich.table import Table

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
from .models import StatFilter
from .scorer import normalize_price, score_listings

console = Console()


@click.group()
@click.pass_context
def main(ctx: click.Context) -> None:
    """poe2market — weighted trade search and deal finder for PoE2."""
    ctx.ensure_object(dict)
    ctx.obj["config"] = Config.load()


# ── config ──────────────────────────────────────────────────────────────


@main.command()
@click.option("--poesessid", default=None, help="POESESSID cookie value.")
@click.option("--league", default=None, help="League name.")
@click.option("--max-fetch", default=None, type=int, help="Max items to fetch.")
@click.option("--tracker-minutes", default=None, type=int,
              help="Sale-tracker poll interval in minutes (0 disables). Applies live "
                   "to a running server within one cycle.")
@click.pass_context
def config(
    ctx: click.Context,
    poesessid: str | None,
    league: str | None,
    max_fetch: int | None,
    tracker_minutes: int | None,
) -> None:
    """View or update configuration."""
    cfg = ctx.obj["config"]
    changed = False
    if poesessid is not None:
        cfg.poesessid = poesessid
        changed = True
    if league is not None:
        cfg.league = league
        changed = True
    if max_fetch is not None:
        cfg.max_fetch_items = max_fetch
        changed = True
    if tracker_minutes is not None:
        cfg.tracker_minutes = max(0, tracker_minutes)
        changed = True

    if changed:
        cfg.save()
        console.print("[green]Config saved.[/green]")
    else:
        table = Table(title="Current Configuration")
        table.add_column("Key")
        table.add_column("Value")
        table.add_row("poesessid", cfg.poesessid[:8] + "..." if cfg.poesessid else "(not set)")
        table.add_row("league", cfg.league)
        table.add_row("cache_dir", cfg.cache_dir)
        table.add_row("cache_ttl_hours", str(cfg.cache_ttl_hours))
        table.add_row("max_fetch_items", str(cfg.max_fetch_items))
        table.add_row("tracker_minutes", str(cfg.tracker_minutes))
        console.print(table)


# ── stats ───────────────────────────────────────────────────────────────


@main.command()
@click.argument("query")
@click.option("--limit", "-n", default=20, help="Max results to show.")
@click.pass_context
def stats(ctx: click.Context, query: str, limit: int) -> None:
    """Search available stat filters by name."""
    cfg = ctx.obj["config"]
    cache = Cache(cfg.cache_dir, cfg.cache_ttl_hours)

    cached = cache.get("stats_data")
    if cached is not None:
        stats_data = cached
    else:
        with TradeAPI(cfg) as api:
            stats_data = api.get_stats()
        cache.set("stats_data", stats_data)

    matches = find_stats(query, stats_data)
    if not matches:
        console.print(f"[yellow]No stats matching '{query}'[/yellow]")
        return

    table = Table(title=f"Stats matching '{query}'")
    table.add_column("ID", style="dim")
    table.add_column("Text")

    for stat_id, text in matches[:limit]:
        table.add_row(stat_id, text)

    console.print(table)
    if len(matches) > limit:
        console.print(f"[dim]({len(matches) - limit} more not shown)[/dim]")


# ── categories ──────────────────────────────────────────────────────────


@main.command()
@click.pass_context
def categories(ctx: click.Context) -> None:
    """List available item categories."""
    cfg = ctx.obj["config"]
    cache = Cache(cfg.cache_dir, cfg.cache_ttl_hours)

    cached = cache.get("items_data")
    if cached is not None:
        items_data = cached
    else:
        with TradeAPI(cfg) as api:
            items_data = api.get_items()
        cache.set("items_data", items_data)

    table = Table(title="Item Categories")
    table.add_column("Category")
    table.add_column("Types")

    for group in items_data:
        label = group.get("label", group.get("id", "?"))
        entries = group.get("entries", [])
        types = ", ".join(e.get("type", "?") for e in entries[:8])
        if len(entries) > 8:
            types += f" (+{len(entries) - 8} more)"
        table.add_row(label, types)

    console.print(table)


# ── analyze ─────────────────────────────────────────────────────────────


@main.command()
@click.argument("category")
@click.option("--min-price", type=float, default=None, help="Min price filter (divine).")
@click.option("--max-price", type=float, default=None, help="Max price filter (divine).")
@click.option("--max-items", type=int, default=None, help="Override max fetch count.")
@click.option("--min-occurrence", type=float, default=0.1, help="Min fraction of items a stat must appear in.")
@click.pass_context
def analyze(
    ctx: click.Context,
    category: str,
    min_price: float | None,
    max_price: float | None,
    max_items: int | None,
    min_occurrence: float,
) -> None:
    """Analyze stat-price relationships for a market segment.

    CATEGORY is the item category (e.g. "armour.body", "weapon.staff").
    Use 'poe2market categories' to list available categories.
    """
    cfg = ctx.obj["config"]

    console.print(
        f"Sampling [bold]{category}[/bold] across price bands in {cfg.league}..."
    )

    with TradeAPI(cfg) as api:
        raw_items = api.search_and_fetch_price_diverse(
            category, max_items=max_items
        )

    if not raw_items:
        console.print("[yellow]No items found.[/yellow]")
        return

    listings = [parse_listing(r) for r in raw_items]
    console.print(f"Fetched {len(listings)} listings. Running regression...")

    from .stash import fetch_exalted_rates

    rates, _ = fetch_exalted_rates(league=cfg.league)
    coefficients = fit_price_model(
        listings, min_occurrence=min_occurrence, chaos_rates=rates
    )
    if not coefficients:
        console.print("[yellow]Not enough stat variance for regression.[/yellow]")
        return

    table = Table(title=f"Stat Price Impact ({category})")
    table.add_column("Stat", max_width=50)
    table.add_column("Exalted / pt", justify="right")
    table.add_column("Std Error", justify="right", style="dim")
    table.add_column("Appearances", justify="right", style="dim")

    for c in coefficients:
        color = "green" if c.coefficient > 0 else "red"
        table.add_row(
            c.text,
            f"[{color}]{c.coefficient:+.2f}[/{color}]",
            f"{c.std_error:.2f}",
            str(c.sample_count),
        )

    console.print(table)


# ── deals ───────────────────────────────────────────────────────────────


def _parse_stat_weight(value: str) -> tuple[str, float]:
    """Parse 'stat name:weight' into (name, weight)."""
    if ":" not in value:
        return value.strip(), 1.0
    name, w = value.rsplit(":", 1)
    try:
        return name.strip(), float(w)
    except ValueError:
        return value.strip(), 1.0


@main.command()
@click.argument("category")
@click.option(
    "--stat", "-s", "stat_specs", multiple=True, required=True,
    help="Stat and weight as 'stat name:weight' (e.g. 'maximum life:3').",
)
@click.option("--min-price", type=float, default=None)
@click.option("--max-price", type=float, default=None)
@click.option("--max-items", type=int, default=None)
@click.option("--limit", "-n", type=int, default=20, help="Max deals to show.")
@click.pass_context
def deals(
    ctx: click.Context,
    category: str,
    stat_specs: tuple[str, ...],
    min_price: float | None,
    max_price: float | None,
    max_items: int | None,
    limit: int,
) -> None:
    """Find best-value items for your stat priorities.

    CATEGORY is the item category. Use --stat/-s for each desired stat
    with an optional importance weight (default 1.0).

    Examples:

        poe2market deals armour.body -s "maximum life:3" -s "fire resistance:1"

        poe2market deals weapon.staff -s "spell damage:2" -s "cast speed:1.5"
    """
    cfg = ctx.obj["config"]
    cache = Cache(cfg.cache_dir, cfg.cache_ttl_hours)

    # Resolve stat names → IDs
    cached_stats = cache.get("stats_data")
    if cached_stats is not None:
        stats_data = cached_stats
    else:
        with TradeAPI(cfg) as api:
            stats_data = api.get_stats()
        cache.set("stats_data", stats_data)

    weights: dict[str, float] = {}
    stat_filters: list[StatFilter] = []
    resolved_names: dict[str, str] = {}

    for spec in stat_specs:
        name, weight = _parse_stat_weight(spec)
        result = resolve_stat(name, stats_data)
        if result is None:
            console.print(f"[red]Could not resolve stat '{name}'.[/red]")
            console.print("Try: poe2market stats \"" + name + "\"")
            sys.exit(1)
        stat_id, text = result
        weights[stat_id] = weight
        resolved_names[stat_id] = text
        stat_filters.append(StatFilter(stat_id=stat_id))
        console.print(f"  {name} → [cyan]{text}[/cyan] (weight {weight})")

    query = build_search_query(
        category=category,
        stat_filters=stat_filters,
        min_price=min_price,
        max_price=max_price,
    )

    console.print(f"\nSearching [bold]{category}[/bold] in {cfg.league} (offline only)...")

    with TradeAPI(cfg) as api:
        raw_items = [r for r in api.search_and_fetch(query, max_items) if is_offline(r)]

    if not raw_items:
        console.print("[yellow]No items found.[/yellow]")
        return

    listings = [parse_listing(r) for r in raw_items]
    console.print(f"Fetched {len(listings)} offline listings. Scoring...")

    from .stash import fetch_exalted_rates

    rates, _ = fetch_exalted_rates(league=cfg.league)
    ranked = score_listings(listings, weights, chaos_rates=rates)
    if not ranked:
        console.print("[yellow]No items matched the requested stats.[/yellow]")
        return

    table = Table(title=f"Top Deals — {category}")
    table.add_column("#", justify="right", style="dim")
    table.add_column("Item")
    table.add_column("Price", justify="right")
    table.add_column("Score", justify="right")
    table.add_column("Value Ratio", justify="right")
    table.add_column("Stat Breakdown", max_width=60)

    for i, deal in enumerate(ranked[:limit], 1):
        item_name = deal.listing.name or deal.listing.base_type or "?"
        ex = normalize_price(deal.listing.price, deal.listing.currency, rates)

        breakdown_parts = []
        for sid, contrib in sorted(
            deal.stat_contributions.items(), key=lambda x: x[1], reverse=True
        ):
            stat_text = resolved_names.get(sid, sid)
            # Shorten for display
            short = stat_text.replace("+# to ", "").replace("+#% to ", "")[:25]
            breakdown_parts.append(f"{short}: {contrib:.0f}")
        breakdown = " | ".join(breakdown_parts)

        ratio_color = "green" if deal.value_ratio > 1 else "yellow"

        table.add_row(
            str(i),
            f"{item_name}\n[dim]{deal.listing.base_type}[/dim]" if deal.listing.name else item_name,
            f"{ex:.0f} ex" if ex == int(ex) else f"{ex:.1f} ex",
            f"{deal.weighted_score:.0f}",
            f"[{ratio_color}]{deal.value_ratio:.2f}[/{ratio_color}]",
            breakdown,
        )

    console.print(table)

    # Whisper for top deal
    top = ranked[0]
    if top.listing.whisper:
        console.print(f"\n[dim]Whisper for #1:[/dim] {top.listing.whisper}")


# ── history ─────────────────────────────────────────────────────────────


@main.command()
@click.option("--no-sync", is_flag=True, help="Show stored history without fetching new sales.")
@click.option("--limit", "-n", default=25, help="Rows to display.")
@click.pass_context
def history(ctx: click.Context, no_sync: bool, limit: int) -> None:
    """Sync and display your merchant sale history."""
    from .history import HistoryStore, summarize

    cfg = ctx.obj["config"]
    store = HistoryStore(cfg.cache_dir)

    if not no_sync:
        if not cfg.poesessid:
            console.print("[red]POESESSID not set.[/red] Run: poe2market config --poesessid ...")
            sys.exit(1)
        console.print(f"Syncing sales for [bold]{cfg.league}[/bold]...")
        with TradeAPI(cfg) as api:
            raw = api.get_history(cfg.league)
        new = store.upsert_many(cfg.league, raw)
        console.print(f"Fetched {len(raw)} · [green]{new} new[/green] · {store.count(cfg.league)} stored total")

    sales = store.all(cfg.league)
    if not sales:
        console.print("[yellow]No stored sales. Open the in-game history, then run again.[/yellow]")
        return

    from .stash import fetch_exalted_rates

    rates, divine_price = fetch_exalted_rates(league=cfg.league)
    summary = summarize(sales, rates)
    console.print(
        f"\n[bold]{summary.count} sales[/bold] · total "
        f"[yellow]{summary.total_divine:.1f} div[/yellow] "
        f"({summary.total_exalted:,.0f} ex) · avg {summary.avg_exalted:,.0f} ex · "
        f"biggest {summary.max_sale_exalted:,.0f} ex ({summary.max_sale_label})"
    )

    table = Table(title=f"Recent Sales — {cfg.league}")
    table.add_column("Time", style="dim")
    table.add_column("Item")
    table.add_column("Price", justify="right")
    table.add_column("Exalted eq.", justify="right")

    for s in sales[:limit]:
        name = s.name or s.base_type or "?"
        table.add_row(
            (s.time or "")[:16].replace("T", " "),
            name,
            f"{s.amount:g} {s.currency}",
            f"{s.exalted_value(rates):,.0f} ex",
        )
    console.print(table)


# ── serve ───────────────────────────────────────────────────────────────


@main.command()
@click.option("--host", default="127.0.0.1", help="Host to bind.")
@click.option("--port", default=8000, type=int, help="Port to bind.")
@click.option("--reload", is_flag=True, help="Auto-reload on file changes.")
@click.option("--open/--no-open", "open_browser", default=True, help="Open browser automatically.")
def serve(host: str, port: int, reload: bool, open_browser: bool) -> None:
    """Launch the local web UI."""
    from . import web

    url = f"http://{host}:{port}"
    console.print(f"[green]Starting poe2market at[/green] [bold]{url}[/bold]")

    if open_browser and not reload:
        import threading
        import time
        import webbrowser

        def _open() -> None:
            time.sleep(1.0)
            webbrowser.open(url)

        threading.Thread(target=_open, daemon=True).start()

    web.run(host=host, port=port, reload=reload)


@main.command()
@click.argument("action", type=click.Choice(["report", "add"]), default="report")
@click.option("--name", default="", help="Item's unique rare name (bonus match key).")
@click.option("--spec", "spec_json", default=None, help="Full item spec JSON (same as appraise).")
@click.option("--low", default=0.0, type=float, help="Predicted low (divine).")
@click.option("--high", default=0.0, type=float, help="Predicted high (divine).")
@click.option("--note", default="", help="Reasoning note.")
@click.pass_context
def predictions(ctx: click.Context, action: str, name: str, spec_json: str | None,
                low: float, high: float, note: str) -> None:
    """Record appraisal price guesses and grade them against actual sales.

    report — show predicted-vs-actual for everything that has sold (default).
    add    — save one guess with the full item spec (matched later by exact stats
             AND name): --spec '{...}' --name "Corpse League" --low 60 --high 80
    """
    import json as _json

    from . import predictions as _pred

    cfg = ctx.obj["config"]
    if action == "add":
        if not spec_json:
            raise click.UsageError("--spec '<json>' is required to add a prediction")
        spec = _json.loads(spec_json)
        _pred.PredictionStore(cfg.cache_dir).add(cfg.league, name, spec, low, high, note)
        click.echo(f"recorded: {name or spec.get('base','?')}  {low:.0f}-{high:.0f}d")
    else:
        _pred.print_report(cfg)


@main.command()
@click.option("--json", "json_spec", default=None, help="Item spec as JSON (else read stdin).")
@click.pass_context
def appraise(ctx: click.Context, json_spec: str | None) -> None:
    """Gather pricing + craft evidence for a boots item spec (JSON).

    Used by the /appraise skill: the model parses the pasted in-game item into a
    spec and pipes it here; this prints comparable sales, tracker clearing data,
    and live offline listings as JSON for the model to reason over.
    """
    import json as _json
    import sys as _sys

    from . import appraise as _appraise

    spec = _json.loads(json_spec) if json_spec else _json.loads(_sys.stdin.read())
    cfg = ctx.obj["config"]
    click.echo(_json.dumps(_appraise.appraise(cfg, spec), indent=2))


@main.command()
@click.argument("action", type=click.Choice(["poll", "report", "watch"]), default="report")
@click.option("--interval", type=float, default=13.0, help="Minutes between polls (watch).")
@click.pass_context
def track(ctx: click.Context, action: str, interval: float) -> None:
    """Track boots listings over time to infer real clearing prices.

    poll   — run one offline poll now and update the lifecycle store.
    report — show inferred clearing prices per mod-bucket (default).
    watch  — poll on a recurring interval until stopped.
    """
    from . import tracker

    cfg = ctx.obj["config"]
    if action == "poll":
        tracker._print_poll(tracker.run_poll(cfg))
    elif action == "watch":
        tracker.watch(cfg, interval)
    else:
        tracker.print_report(cfg)


if __name__ == "__main__":
    main()
