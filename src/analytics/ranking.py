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
    overall_score: int | None = None
    deal_score: int | None = None
    confidence_score: int = 0
    liquidity_score: int = 0
    risk_score: int = 0
    priority: str = "reject"
    verdict: str = "INSUFFICIENT DATA"
    needs_review: bool = False
    requires_review: bool = False
    score_reasons: tuple[str, ...] = ()
    risk_reasons: tuple[str, ...] = ()
    review_reasons: tuple[str, ...] = ()
    target_buy_price: int | None = None
    max_buy_price: int | None = None
    raw_score_band: str = "INSUFFICIENT DATA"


def assess_resale(*, asking_price: int | None, median: float | None,
                  q1: float | None, sample_count: int, first_seen: datetime | None,
                  trend_7d: float | None = None, trend_30d: float | None = None,
                  activity_count: int = 0, target_price: int | None = None,
                  condition_class: str = "OK", retail_price: int | None = None,
                  retail_confidence: str = "insufficient",
                  retail_stale: bool = False, match_confidence: str = "high",
                  multi_item: bool = False, price_ambiguous: bool = False,
                  ram_compatibility_unknown: bool = False,
                  source_degraded: bool = False, geography_broadened: bool = False,
                  item_condition: str = "unknown", liquidity_fallback: int = 50,
                  comparable_fallback: bool = False,
                  historical_fallback_used: bool = False) -> ResaleAssessment:
    trend = _trend_label(trend_7d if trend_7d is not None else trend_30d)
    if asking_price is None or median is None or sample_count < 3:
        review = ["insufficient compatible market data"]
        risks: list[str] = []
        if multi_item:
            risks.append("listing contains multiple products")
            review.append("listing contains multiple products")
        if price_ambiguous:
            risks.append("listing-level price cannot be assigned to one product")
            review.append("listing-level price cannot be assigned to one product")
        if condition_class == "FAULT":
            risks.append("faulty or parts-only condition")
            review.append("faulty or parts-only condition")
        elif condition_class == "RISK":
            risks.append("condition wording indicates elevated risk")
            review.append("condition wording indicates elevated risk")
        final = "REJECT" if condition_class == "FAULT" else "NEEDS REVIEW"
        return ResaleAssessment(None, final, None, None, None,
                                "insufficient", trend, trend_7d, trend_30d,
                                overall_score=None,
                                risk_score=100 if condition_class == "FAULT" else 60 if risks else 0,
                                priority="reject" if condition_class == "FAULT" else "4 / needs_review",
                                verdict="REJECT" if condition_class == "FAULT" else "NEEDS REVIEW",
                                needs_review=True, requires_review=True,
                                risk_reasons=_unique(risks), review_reasons=_unique(review),
                                raw_score_band="INSUFFICIENT DATA")
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
    deal_score = int(round(max(0.0, min(100.0, discount_points + margin_points
                                       + trend_adjustment + hint + retail_adjustment + 18))))
    confidence_score = min(100, 30 + min(sample_count, 10) * 6
                           + (10 if match_confidence == "high" else 0))
    review: list[str] = []
    risks: list[str] = []
    reasons = [f"price is {abs(discount):.1f}% {'below' if discount >= 0 else 'above'} compatible used median",
               f"{sample_count} compatible competitor listings"]
    if comparable_fallback:
        confidence_score -= 15
        reasons.append("exact VRAM sample insufficient; compatible model-level market used")
    if historical_fallback_used:
        confidence_score -= 12
        reasons.append("active sample insufficient; recent historical observations included")
    if source_degraded:
        confidence_score -= 20
        reasons.append("source candidate quality is degraded")
    if match_confidence not in {"high", "medium"}:
        confidence_score -= 25
        if match_confidence == "insufficient":
            review.append("exact normalized product is unknown")
        else:
            reasons.append("normalized product identity has moderate uncertainty")
    if item_condition == "unknown":
        confidence_score -= 10
        reasons.append("condition is not sufficiently established")
    if ram_compatibility_unknown:
        confidence_score -= 18
        reasons.append("UDIMM/RDIMM or ECC status is unknown")
    if multi_item:
        risks.append("listing contains multiple products")
        review.append("listing contains multiple products")
    if price_ambiguous:
        risks.append("listing-level price cannot be assigned to one product")
        review.append("listing-level price cannot be assigned to one product")
    if condition_class == "FAULT":
        risks.append("faulty or parts-only condition")
        review.append("faulty or parts-only condition")
    elif condition_class == "RISK":
        risks.append("condition wording indicates elevated risk")
        review.append("condition wording indicates elevated risk")
    if discount >= 45:
        risks.append("extreme discount anomaly requires verification")
        review.append("price anomaly requires manual verification")
    risk_score = min(100, (65 if condition_class == "FAULT" else 25 if condition_class == "RISK" else 8)
                     + (25 if multi_item else 0) + (25 if price_ambiguous else 0)
                     + (20 if discount >= 45 else 0) + (10 if ram_compatibility_unknown else 0)
                     + (10 if item_condition == "unknown" else 0))
    observed_liquidity = min(100, 35 + min(sample_count, 10) * 4 + min(activity_count, 10) * 2)
    liquidity_score = int(round(observed_liquidity * .75 + liquidity_fallback * .25))
    confidence_score = max(0, min(100, confidence_score))
    score = int(round(max(0, min(100, deal_score * .45 + confidence_score * .2
                                 + liquidity_score * .2 + (100 - risk_score) * .15))))
    needs_review = bool(review)
    reliable = not (multi_item or price_ambiguous or condition_class == "FAULT"
                    or match_confidence == "insufficient")
    if not reliable:
        resale = margin = None
    max_buy = int(round(resale * .78)) if resale is not None else None
    target_buy = int(round(resale * .68)) if resale is not None else None
    economics_positive = (margin is not None and margin > 0 and resale is not None
                          and resale > asking_price and max_buy is not None)
    above_max = max_buy is not None and asking_price > max_buy
    if margin is not None and margin <= 0:
        risks.append("expected resale margin is negative")
        reasons.append("expected resale margin is negative")
    if above_max:
        reasons.append("asking price exceeds calculated max buy price")
    if condition_class == "FAULT":
        priority, verdict = "reject", "REJECT"
    elif needs_review:
        priority, verdict = "4 / needs_review", "NEEDS REVIEW"
    elif (economics_positive and asking_price <= target_buy and score >= 82
          and confidence_score >= 70 and risk_score <= 35):
        priority, verdict = "1", "STRONG DEAL"
    elif (economics_positive and asking_price <= max_buy and score >= 68
          and confidence_score >= 55 and risk_score <= 50):
        priority, verdict = "2", "GOOD DEAL"
    elif (max_buy is not None and asking_price <= round(max_buy * 1.15)
          and target_buy is not None and score >= 48):
        reasons.append("deal becomes attractive only after negotiation")
        priority, verdict = "3", "NEGOTIATE"
    else:
        priority, verdict = "reject", "PASS"
    raw_score_band = "BUY" if score >= 80 else "GOOD DEAL" if score >= 65 else "NEGOTIATE" if score >= 45 else "PASS"
    recommendation = verdict
    return ResaleAssessment(score, recommendation, round(discount, 1), resale, margin,
                            confidence, trend, trend_7d, trend_30d,
                            round(retail_discount, 1) if retail_discount is not None else None,
                            round(used_new_gap, 1) if used_new_gap is not None else None,
                            score, deal_score, confidence_score, liquidity_score, risk_score,
                            priority, verdict, needs_review, needs_review, _unique(reasons), _unique(risks),
                            _unique(review), target_buy, max_buy, raw_score_band)


def _trend_label(value: float | None) -> str:
    if value is None or abs(value) < 3:
        return "STABLE"
    return "RISING" if value > 0 else "FALLING"


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _unique(values: list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))
