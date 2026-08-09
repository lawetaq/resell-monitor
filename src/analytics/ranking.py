from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass(slots=True, frozen=True)
class ResaleAssessment:
    score: int | None
    recommendation: str
    market_discount_percent: float | None
    estimated_resale_price: int | None
    expected_gross_margin: int | None
    confidence: str
    market_trend: str
    trend_7d_percent: float | None = None
    trend_30d_percent: float | None = None
    listing_discount_vs_retail: float | None = None
    used_new_gap_percent: float | None = None


def assess_resale(*, asking_price: int | None, median: float | None,
                  q1: float | None, sample_count: int, first_seen: datetime | None,
                  trend_7d: float | None = None, trend_30d: float | None = None,
                  activity_count: int = 0, target_price: int | None = None,
                  condition_class: str = "OK", retail_price: int | None = None,
                  retail_confidence: str = "insufficient",
                  retail_stale: bool = False) -> ResaleAssessment:
    trend = _trend_label(trend_7d if trend_7d is not None else trend_30d)
    if asking_price is None or median is None or sample_count < 3:
        return ResaleAssessment(None, "INSUFFICIENT DATA", None, None, None,
                                "insufficient", trend, trend_7d, trend_30d)
    confidence = "high" if sample_count >= 10 else "medium" if sample_count >= 5 else "low"
    discount = (median - asking_price) / median * 100
    # Q1 is a prudent resale baseline; never imply the full median is guaranteed.
    resale = int(round((q1 if q1 is not None else median * .85) * .97))
    if trend == "FALLING":
        resale = int(round(resale * .95))
    margin = resale - asking_price
    discount_points = max(0.0, min(60.0, discount * 1.7))
    margin_ratio = margin / asking_price * 100 if asking_price else 0
    margin_points = max(0.0, min(22.0, margin_ratio * .55))
    liquidity_points = min(8.0, activity_count * .8)
    freshness_points = 0.0
    if first_seen:
        age_days = max(0.0, (datetime.now(timezone.utc) - _aware(first_seen)).total_seconds() / 86400)
        freshness_points = max(0.0, 7.0 - age_days)
    confidence_points = {"low": 0.0, "medium": 2.0, "high": 5.0}[confidence]
    trend_adjustment = -9.0 if trend == "FALLING" else 2.0 if trend == "RISING" else 0.0
    condition_adjustment = -10.0 if condition_class == "FAULT" else -5.0 if condition_class == "RISK" else 0.0
    hint = 0.0
    if target_price and asking_price <= target_price:
        hint = 2.0
    retail_discount = None
    used_new_gap = None
    retail_adjustment = 0.0
    if retail_price and retail_price > 0:
        retail_discount = (retail_price - asking_price) / retail_price * 100
        used_new_gap = (median - retail_price) / retail_price * 100
        # Retail is corroborating evidence only. One stale/noisy offer contributes nothing.
        weight = 1.0 if retail_confidence == "high" else .5 if retail_confidence == "medium" else 0.0
        if retail_stale: weight *= .25
        if weight:
            if retail_price <= median * 1.08:
                retail_adjustment = -6.0 * weight
            elif retail_discount >= 30 and retail_price >= median * 1.2:
                retail_adjustment = 4.0 * weight
    score = int(round(max(0.0, min(100.0, discount_points + margin_points + liquidity_points + freshness_points + confidence_points + trend_adjustment + condition_adjustment + hint + retail_adjustment))))
    recommendation = "BUY" if score >= 80 else "GOOD DEAL" if score >= 65 else "NEGOTIATE" if score >= 45 else "PASS"
    return ResaleAssessment(score, recommendation, round(discount, 1), resale, margin,
                            confidence, trend, trend_7d, trend_30d,
                            round(retail_discount, 1) if retail_discount is not None else None,
                            round(used_new_gap, 1) if used_new_gap is not None else None)


def _trend_label(value: float | None) -> str:
    if value is None or abs(value) < 3:
        return "STABLE"
    return "RISING" if value > 0 else "FALLING"


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
