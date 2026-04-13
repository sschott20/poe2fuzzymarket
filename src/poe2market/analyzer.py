from collections import Counter

import numpy as np

from .models import Listing, StatCoefficient
from .scorer import normalize_price


def get_common_stats(
    listings: list[Listing], min_fraction: float = 0.1
) -> list[str]:
    """Return stat IDs that appear in at least `min_fraction` of listings."""
    counts: Counter[str] = Counter()
    for listing in listings:
        seen: set[str] = set()
        for sv in listing.stats:
            if sv.stat_id not in seen:
                counts[sv.stat_id] += 1
                seen.add(sv.stat_id)

    threshold = max(1, int(len(listings) * min_fraction))
    return [sid for sid, c in counts.items() if c >= threshold]


def fit_price_model(
    listings: list[Listing],
    stat_ids: list[str] | None = None,
    min_occurrence: float = 0.1,
    chaos_rates: dict[str, float] | None = None,
) -> list[StatCoefficient]:
    """Fit OLS regression: stat values -> chaos price.

    Returns coefficients sorted by absolute magnitude (most impactful first).
    """
    if stat_ids is None:
        stat_ids = get_common_stats(listings, min_occurrence)
    if not stat_ids or not listings:
        return []

    sid_to_idx = {sid: i for i, sid in enumerate(stat_ids)}
    n = len(listings)
    m = len(stat_ids)

    X = np.zeros((n, m + 1))  # +1 for intercept
    y = np.zeros(n)

    for i, listing in enumerate(listings):
        for sv in listing.stats:
            if sv.stat_id in sid_to_idx:
                X[i, sid_to_idx[sv.stat_id]] = sv.value
        X[i, -1] = 1.0  # intercept
        y[i] = normalize_price(listing.price, listing.currency, chaos_rates)

    # OLS via least squares
    result, residuals, rank, _sv = np.linalg.lstsq(X, y, rcond=None)

    # Standard errors
    std_errors = np.zeros(m + 1)
    if len(residuals) > 0 and n > m + 1:
        mse = residuals[0] / (n - m - 1)
        try:
            cov = mse * np.linalg.inv(X.T @ X)
            diag = np.diagonal(cov)
            std_errors = np.sqrt(np.maximum(diag, 0))
        except np.linalg.LinAlgError:
            pass

    # Build stat text map from listings
    text_map: dict[str, str] = {}
    occurrence: Counter[str] = Counter()
    for listing in listings:
        for sv in listing.stats:
            if sv.stat_id in sid_to_idx:
                text_map.setdefault(sv.stat_id, sv.text)
                occurrence[sv.stat_id] += 1

    coefficients = []
    for sid, idx in sid_to_idx.items():
        coefficients.append(
            StatCoefficient(
                stat_id=sid,
                text=text_map.get(sid, sid),
                coefficient=float(result[idx]),
                std_error=float(std_errors[idx]) if idx < len(std_errors) else 0.0,
                sample_count=occurrence.get(sid, 0),
            )
        )

    coefficients.sort(key=lambda c: abs(c.coefficient), reverse=True)
    return coefficients
