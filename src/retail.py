from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from statistics import median
from typing import Protocol


@dataclass(slots=True, frozen=True)
class RetailPriceObservation:
    comparable_key: str
    retailer: str
    price: int
    observed_at: datetime
    url: str | None = None
    product_title: str | None = None
    normalized_model: str | None = None
    original_price: int | None = None
    seller: str | None = None
    marketplace: str | None = None
    availability: str = "unknown"
    region: str | None = None
    match_confidence: str = "insufficient"
    delivery_price: int | None = None
    offer_id: str | None = None
    product_id: str | None = None
    seller_kind: str | None = None


@dataclass(slots=True, frozen=True)
class RetailRetrievalResult:
    retailer: str
    observations: tuple[RetailPriceObservation, ...]
    status_code: int | None = None
    transport: str = "http"
    health: str = "healthy"
    error: str | None = None
    candidates_found: int = 0
    raw_body: str | None = None
    final_url: str | None = None


@dataclass(slots=True, frozen=True)
class RetailAggregate:
    representative_price: int | None
    cheapest_price: int | None
    offer_count: int
    confidence: str


def aggregate_retail_offers(
    observations: list[RetailPriceObservation],
) -> RetailAggregate:
    credible = [
        item for item in observations
        if item.price > 0
        and item.availability not in {"unavailable", "out_of_stock"}
        and item.match_confidence in {"high", "exact"}
    ]
    if not credible:
        return RetailAggregate(None, None, 0, "insufficient")
    prices = [item.price for item in credible]
    confidence = "high" if len(prices) >= 5 else "medium" if len(prices) >= 2 else "low"
    return RetailAggregate(int(round(median(prices))), min(prices), len(prices), confidence)


class RetailPriceProvider(Protocol):
    @property
    def name(self) -> str: ...

    def search(
        self,
        comparable_key: str,
        query: str,
        *,
        region: str | None = None,
        mapped_url: str | None = None,
    ) -> RetailRetrievalResult: ...

    def close(self) -> None: ...
