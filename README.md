# poe2market

Weighted trade search and deal finder for Path of Exile 2.

The official trade site lets you set hard thresholds ("life >= 80, fire res >= 30") but doesn't help you answer questions like "which item gives me the most life + resists per chaos?" This tool does that.

## What it does

- **Analyze** a market segment to see how each stat affects price (linear regression over current listings)
- **Find deals** by specifying stats you care about with importance weights, then ranking items by value-per-chaos
- **Search stats** by name to find the right filter IDs

## Install

```bash
pip install -e .
```

## Setup

Grab your `POESESSID` from browser devtools (pathofexile.com → Application → Cookies) and configure:

```bash
poe2market config --poesessid "your_session_id_here"
poe2market config --league "Fate of the Vaal"
```

Or use environment variables:
```bash
export POE2_SESSID="your_session_id_here"
export POE2_LEAGUE="Fate of the Vaal"
```

## Web UI

```bash
poe2market serve
```

Opens a local web interface at `http://localhost:8000` with three tabs:

- **Find Deals** — pick a category, search for stats (with autocomplete), set importance weights, and get a ranked list of best-value listings with stat contribution breakdown and one-click whisper copy. Includes a natural-language prompt box ("body armour with high life and fire resistance under 300 chaos") that uses Claude to fill in the form automatically.
- **Analyze Market** — see a visual breakdown of which stats are driving prices, with regression bars showing relative impact.
- **Settings** — configure POESESSID, league, cache, Anthropic API key.

### Prompt interpretation (optional)

Set an Anthropic API key in Settings (or the `ANTHROPIC_API_KEY` env var) to enable the prompt box on the Deals tab. Type what you want in plain English and Claude picks the category, maps stats with weights, and sets price filters. Defaults to `claude-opus-4-6`; override with any model ID (e.g. `claude-haiku-4-5`) in Settings.

Flags: `--host`, `--port`, `--no-open` (don't open browser), `--reload` (auto-reload on changes).

## CLI Usage

### Search stats

Find the right stat filter names:

```bash
poe2market stats "spell damage"
poe2market stats "maximum life"
```

### List categories

```bash
poe2market categories
```

### Analyze stat-price relationships

See which stats drive prices in a market segment:

```bash
poe2market analyze armour.body --max-price 500
poe2market analyze weapon.staff --min-price 10 --max-price 1000
```

This fetches listings, runs regression, and shows you how much each stat point is worth in chaos.

### Find deals

Specify stats you care about with importance weights and find the best value listings:

```bash
# Weight: how much you value each stat relative to others
poe2market deals armour.body \
  -s "maximum life:3" \
  -s "fire resistance:1" \
  -s "cold resistance:1" \
  --max-price 300

poe2market deals weapon.staff \
  -s "spell damage:2" \
  -s "cast speed:1.5" \
  -s "cold damage:1"
```

Items are ranked by `weighted_stat_score / price` — the higher the ratio, the more stats per chaos you get.

## How it works

**Analyze mode** collects current listings for a category, builds a feature matrix of all stat values, and fits OLS regression to estimate how much each stat point contributes to price. Stats that appear in fewer than 10% of listings are filtered out (configurable with `--min-occurrence`).

**Deals mode** takes your stat preferences with weights, queries the trade API for items with those stats, computes a weighted score for each item, then ranks by score/price. The top results are the items where you get the most of what you want for the least cost.

Prices are normalized to chaos equivalents using approximate exchange rates. The tool caches API responses locally (SQLite) to avoid hammering the rate-limited trade API.

## Rate limits

The trade API is rate-limited (~5 searches per 12 seconds, ~12 fetches per 6 seconds). The tool handles this automatically with a sliding-window rate limiter that adapts to the limits reported in response headers. Expect a full analyze/deals run to take 30-60 seconds depending on how many items you fetch.

## Dev

```bash
pip install -e ".[dev]"
pytest
```
