from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(slots=True, frozen=True)
class MarketSnapshot:
    comparable_key: str
    snapshot_at: datetime
    median: float
    q1: float
    q3: float
    sample_count: int
    new_listings_count: int = 0
    price_drop_count: int = 0
    disappeared_count: int = 0
    minimum: int | None = None
    maximum: int | None = None
    recent_median: float | None = None


def percentile(values: list[int | float], fraction: float) -> float:
    if not values:
        raise ValueError("cannot calculate percentile without values")
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return float(ordered[lower] * (1 - weight) + ordered[upper] * weight)


def aggregate_market(comparable_key: str, prices: list[int], snapshot_at: datetime,
                     *, new_count: int = 0, drop_count: int = 0,
                     disappeared_count: int = 0,
                     recent_prices: list[int] | None = None) -> MarketSnapshot | None:
    valid = [price for price in prices if price > 0]
    if not valid:
        return None
    q1, median, q3 = percentile(valid, .25), percentile(valid, .5), percentile(valid, .75)
    iqr = q3 - q1
    lower, upper = max(0, q1 - 3 * iqr), q3 + 3 * iqr
    # Keep all observations in sample/quartiles. Bounds only make min/max resistant to extreme noise.
    bounded = [price for price in valid if lower <= price <= upper] or valid
    return MarketSnapshot(comparable_key, snapshot_at, median, q1, q3, len(valid),
                          new_count, drop_count, disappeared_count,
                          min(bounded), max(bounded),
                          percentile(recent_prices, .5) if recent_prices else None)
