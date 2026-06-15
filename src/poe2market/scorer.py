from .models import Deal, Listing

# Approximate chaos-equivalent rates. Users can override via --rates flag.
# These fluctuate with the economy; treat derived totals as estimates.
DEFAULT_CHAOS_RATES: dict[str, float] = {
    "chaos": 1.0,
    "divine": 150.0,
    "exalted": 12.0,
    "annul": 8.0,
    "annulment": 8.0,
    "chance": 0.05,
    "alchemy": 0.2,
    "alch": 0.2,
    "regal": 0.5,
    "vaal": 1.0,
    "fusing": 0.3,
    "jeweller": 0.1,
    "gcp": 1.0,
    "blessed": 0.5,
    "scouring": 0.5,
    "augmentation": 0.05,
    "transmutation": 0.02,
    "wisdom": 0.01,
}

# PoE2 economy is Exalted-based (Exalted = 1), with Divine the high denomination.
# Used as the valuation base for sale history / net worth. These are a static
# fallback; live values come from poe2scout (see stash.fetch_exalted_rates).
DEFAULT_EXALTED_RATES: dict[str, float] = {
    "exalted": 1.0,
    "divine": 124.0,
    "chaos": 12.0,
    "vaal": 2.4,
    "annul": 66.0,
    "annulment": 66.0,
    "regal": 2.0,
    "chance": 0.2,
    "alch": 0.5,
    "alchemy": 0.5,
    "aug": 0.05,
    "augmentation": 0.05,
    "transmute": 0.02,
    "transmutation": 0.02,
    "regret": 1.0,
    "mirror": 388000.0,
}


def normalize_price(
    amount: float, currency: str, rates: dict[str, float] | None = None
) -> float:
    """Convert a price to chaos equivalent."""
    r = rates or DEFAULT_CHAOS_RATES
    return amount * r.get(currency, 1.0)


def score_listings(
    listings: list[Listing],
    weights: dict[str, float],
    chaos_rates: dict[str, float] | None = None,
) -> list[Deal]:
    """Score and rank listings by weighted stat value per chaos spent.

    Args:
        listings: Parsed trade listings.
        weights: Map of stat_id -> importance weight.
        chaos_rates: Optional currency conversion rates.

    Returns:
        Deals sorted by value_ratio (best deals first).
    """
    deals: list[Deal] = []

    for listing in listings:
        chaos_price = normalize_price(listing.price, listing.currency, chaos_rates)
        if chaos_price <= 0:
            continue

        contributions: dict[str, float] = {}
        total = 0.0

        for sv in listing.stats:
            if sv.stat_id in weights:
                contrib = sv.value * weights[sv.stat_id]
                contributions[sv.stat_id] = contrib
                total += contrib

        if total <= 0:
            continue

        deals.append(
            Deal(
                listing=listing,
                weighted_score=total,
                value_ratio=total / chaos_price,
                stat_contributions=contributions,
            )
        )

    deals.sort(key=lambda d: d.value_ratio, reverse=True)
    return deals
