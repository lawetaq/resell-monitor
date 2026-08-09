from __future__ import annotations

from datetime import datetime, timezone
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
                comparable_key: str, region: str | None) -> RetailRetrievalResult:
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
                region=region, match_confidence="exact",
                delivery_price=_int(candidate.get("delivery_price")),
                offer_id=_text(candidate.get("offer_id")),
                product_id=_text(candidate.get("product_id")),
                seller_kind=_text(candidate.get("seller_kind")),
            ))
        return RetailRetrievalResult(
            self.name, tuple(observations), page.status_code, "requests-persistent",
            "healthy" if page.status_code == 200 else "blocked" if page.status_code in {401, 403, 429} else "degraded",
            candidates_found=len(candidates), raw_body=page.text, final_url=page.final_url,
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
