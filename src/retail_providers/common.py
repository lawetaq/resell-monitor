from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Callable
from urllib.parse import quote_plus

from src.analytics import normalize_product
from src.retail import RetailPriceObservation, RetailRetrievalResult
from src.sources.http import HttpPage, HttpTransport

Fetcher = Callable[[str], HttpPage]


def exact_match(comparable_key: str, title: str) -> tuple[bool, str | None]:
    lowered = title.casefold()
    if "ноутбук" in lowered or "laptop" in lowered:
        return False, None
    product = normalize_product(title)
    return product.comparable_key == comparable_key, product.normalized_model


class HttpRetailProvider:
    name = "retailer"
    search_url = ""

    def __init__(self, *, transport: HttpTransport | None = None,
                 fetcher: Fetcher | None = None) -> None:
        self._transport = transport or (None if fetcher else HttpTransport())
        self._fetcher = fetcher or (lambda url: self._transport.fetch(  # type: ignore[union-attr]
            url, accept_error_status=True
        ))

    def _url(self, query: str, region: str | None, mapped_url: str | None) -> str:
        return mapped_url or self.search_url.format(query=quote_plus(query), region=quote_plus(region or ""))

    def _result(self, page: HttpPage, candidates: list[dict[str, object]],
                comparable_key: str, region: str | None, *,
                retrieval_method: str = "search",
                region_context: str | None = None) -> RetailRetrievalResult:
        observations: list[RetailPriceObservation] = []
        now = datetime.now(timezone.utc)
        for candidate in candidates:
            title = str(candidate.get("title") or "")
            matched, model = exact_match(comparable_key, title)
            if not matched:
                continue
            observations.append(RetailPriceObservation(
                comparable_key=comparable_key, retailer=self.name,
                price=int(candidate["price"]), observed_at=now,
                url=str(candidate.get("url") or page.final_url),
                product_title=title, normalized_model=model,
                original_price=_int(candidate.get("original_price")),
                seller=_text(candidate.get("seller")), marketplace=self.name,
                availability=str(candidate.get("availability") or "available"),
                region=region_context or region, match_confidence="exact",
                delivery_price=_int(candidate.get("delivery_price")),
                offer_id=_text(candidate.get("offer_id")),
                product_id=_text(candidate.get("product_id")),
                seller_kind=_text(candidate.get("seller_kind")),
                conditional_price=_int(candidate.get("conditional_price")),
            ))
        blocked = page.status_code in {401, 403, 429, 498}
        return RetailRetrievalResult(
            self.name, tuple(observations), page.status_code, "requests-persistent",
            "healthy" if page.status_code == 200 else "blocked" if blocked else "degraded",
            error=(f"HTTP {page.status_code}" if page.status_code != 200 else None),
            candidates_found=len(candidates), raw_body=page.text, final_url=page.final_url,
            retrieval_method=retrieval_method,
            region_context=region_context or region,
            retry_after_seconds=parse_retry_after(page.retry_after),
            block_classification=("rate_limited" if page.status_code == 429
                                  else "challenge" if page.status_code == 498
                                  else "access_blocked" if blocked else "none"),
            response_content_type=page.content_type,
            response_size=page.response_size if page.response_size is not None
            else len(page.text.encode("utf-8")),
        )

    def close(self) -> None:
        if self._transport:
            self._transport.close()

    def __enter__(self):
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def _int(value: object) -> int | None:
    if value in (None, ""):
        return None
    return int(float(str(value)))


def _text(value: object) -> str | None:
    return str(value) if value not in (None, "") else None


def parse_retry_after(value: str | None, *, now: datetime | None = None) -> int | None:
    if not value:
        return None
    stripped = value.strip()
    if stripped.isdigit():
        return max(0, int(stripped))
    try:
        target = parsedate_to_datetime(stripped)
    except (TypeError, ValueError):
        return None
    if target.tzinfo is None:
        target = target.replace(tzinfo=timezone.utc)
    return max(0, int((target - (now or datetime.now(timezone.utc))).total_seconds()))
