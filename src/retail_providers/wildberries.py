from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from typing import Mapping
from urllib.parse import parse_qs, urlencode, urlsplit

from src.retail import RetailRetrievalResult
from src.sources.http import HttpTransport, HttpTransportError

from .common import HttpRetailProvider


SEARCH_ENDPOINT = "https://search.wb.ru/exactmatch/ru/common/v18/search"
CARD_ENDPOINT = "https://card.wb.ru/cards/v4/detail"
WB_REFERER = "https://www.wildberries.ru/"
WB_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.5",
    "Referer": WB_REFERER,
}


@dataclass(slots=True, frozen=True)
class WildberriesRegionContext:
    configured_region: str | None
    destination: str | None

    @property
    def persisted(self) -> str:
        name = self.configured_region or "unspecified"
        destination = self.destination or "unresolved"
        return f"{name}; wb_dest={destination}"


def resolve_wildberries_region(region: str | None) -> WildberriesRegionContext:
    """Resolve only explicit WB destination IDs; never pretend a city name is one."""

    cleaned = (region or "").strip()
    if re.fullmatch(r"-?\d+", cleaned):
        return WildberriesRegionContext(region, cleaned)
    match = re.search(r"(?:wb[_-]?dest|dest)\s*[:=]\s*(-?\d+)", cleaned, re.I)
    return WildberriesRegionContext(region, match.group(1) if match else None)


def extract_product_id(mapped_url: str | None) -> str | None:
    if not mapped_url:
        return None
    stripped = mapped_url.strip()
    if stripped.isdigit():
        return stripped
    parsed = urlsplit(stripped)
    query = parse_qs(parsed.query)
    for key in ("nm", "nmId", "product_id"):
        values = query.get(key)
        if values and values[0].isdigit():
            return values[0]
    match = re.search(r"/catalog/(\d+)(?:/|$)", parsed.path)
    return match.group(1) if match else None


def build_wildberries_url(
    query: str,
    region: str | None,
    mapped_url: str | None = None,
) -> tuple[str, str, WildberriesRegionContext]:
    context = resolve_wildberries_region(region)
    product_id = extract_product_id(mapped_url)
    if mapped_url and product_id is None:
        raise ValueError("Wildberries mapping must contain a numeric catalog nmId")
    common = {"appType": "1", "curr": "rub", "lang": "ru", "spp": "30"}
    if context.destination:
        common["dest"] = context.destination
    if product_id:
        return (
            f"{CARD_ENDPOINT}?{urlencode({**common, 'nm': product_id})}",
            "mapped_product",
            context,
        )
    parameters = {
        **common,
        "ab_testing": "false",
        "query": query,
        "resultset": "catalog",
        "suppressSpellcheck": "false",
    }
    return f"{SEARCH_ENDPOINT}?{urlencode(parameters)}", "search", context


def parse_wildberries(body: str) -> list[dict[str, object]]:
    value = json.loads(body)
    products = _products(value)
    result: list[dict[str, object]] = []
    for product in products:
        product_id = product.get("id") or product.get("nmId") or product.get("nmID")
        if product_id is None:
            continue
        title = product.get("name") or product.get("title")
        seller = product.get("supplier") or product.get("supplierName")
        sizes = product.get("sizes") if isinstance(product.get("sizes"), list) else []
        if sizes:
            for index, size in enumerate(sizes):
                if not isinstance(size, dict):
                    continue
                row = _size_candidate(product, size, index, product_id, title, seller)
                if row is not None:
                    result.append(row)
        else:
            row = _legacy_candidate(product, product_id, title, seller)
            if row is not None:
                result.append(row)
    return result


def _products(value: object) -> list[Mapping[str, object]]:
    if not isinstance(value, dict):
        return []
    data = value.get("data")
    candidates = data.get("products") if isinstance(data, dict) else value.get("products")
    return [item for item in candidates or [] if isinstance(item, dict)]


def _size_candidate(
    product: Mapping[str, object],
    size: Mapping[str, object],
    index: int,
    product_id: object,
    title: object,
    seller: object,
) -> dict[str, object] | None:
    prices = size.get("price") if isinstance(size.get("price"), dict) else {}
    normal = _money(prices.get("product"))
    original = _money(prices.get("basic"))
    conditional = _money(prices.get("total"))
    if normal is None:
        normal = _money(product.get("salePriceU"))
    if normal is None:
        return None
    if conditional is not None and conditional >= normal:
        conditional = None
    offer = (size.get("optionId") or size.get("optionID") or size.get("chrtId")
             or size.get("techSize") or size.get("name") or index)
    quantity = _quantity(size, product)
    return _candidate(product_id, title, seller, normal, original, conditional,
                      offer, quantity)


def _legacy_candidate(
    product: Mapping[str, object], product_id: object, title: object, seller: object
) -> dict[str, object] | None:
    normal = _money(product.get("salePriceU"))
    if normal is None:
        return None
    return _candidate(product_id, title, seller, normal,
                      _money(product.get("priceU")), None, product_id,
                      _quantity({}, product))


def _candidate(product_id: object, title: object, seller: object,
               normal: int, original: int | None, conditional: int | None,
               offer: object, quantity: int | None) -> dict[str, object]:
    return {
        "title": title,
        "price": normal,
        "original_price": original,
        "conditional_price": conditional,
        "url": f"https://www.wildberries.ru/catalog/{product_id}/detail.aspx",
        "product_id": product_id,
        "offer_id": f"{product_id}:{offer}",
        "seller": seller,
        "availability": ("available" if quantity is not None and quantity > 0
                         else "out_of_stock" if quantity == 0 else "unknown"),
    }


def _money(value: object) -> int | None:
    if value in (None, ""):
        return None
    amount = int(value)
    return amount // 100 if amount >= 100 else amount


def _quantity(size: Mapping[str, object], product: Mapping[str, object]) -> int | None:
    stocks = size.get("stocks")
    if isinstance(stocks, list):
        quantities = [int(stock.get("qty", 0)) for stock in stocks
                      if isinstance(stock, dict) and stock.get("qty") is not None]
        if quantities:
            return sum(quantities)
    for source in (size, product):
        value = source.get("totalQuantity")
        if value is not None:
            return int(value)
    return None


class WildberriesRetailProvider(HttpRetailProvider):
    name = "wildberries"

    def __init__(self, *, transport: HttpTransport | None = None,
                 fetcher=None) -> None:
        super().__init__(
            transport=transport or (None if fetcher else HttpTransport(headers=WB_HEADERS)),
            fetcher=fetcher,
        )

    def search(self, comparable_key: str, query: str, *, region: str | None = None,
               mapped_url: str | None = None) -> RetailRetrievalResult:
        url, method, context = build_wildberries_url(query, region, mapped_url)
        try:
            page = self._fetcher(url)
        except HttpTransportError as error:
            return RetailRetrievalResult(
                self.name, (), None, "requests-persistent", "failed",
                error=f"Wildberries request failed: {error}",
                final_url=url, retrieval_method=method,
                region_context=context.persisted,
                block_classification="network_error",
            )
        if page.status_code != 200:
            return self._result(page, [], comparable_key, region,
                                retrieval_method=method,
                                region_context=context.persisted)
        if "json" not in page.content_type.casefold():
            return RetailRetrievalResult(
                self.name, (), page.status_code, "requests-persistent", "degraded",
                error="Wildberries returned non-JSON content",
                raw_body=page.text, final_url=page.final_url,
                retrieval_method=method, region_context=context.persisted,
                response_content_type=page.content_type,
                response_size=page.response_size or len(page.text.encode("utf-8")),
            )
        try:
            candidates = parse_wildberries(page.text)
        except (json.JSONDecodeError, TypeError, ValueError) as error:
            return RetailRetrievalResult(
                self.name, (), page.status_code, "requests-persistent", "degraded",
                error=f"Wildberries response schema could not be parsed: {type(error).__name__}",
                raw_body=page.text, final_url=page.final_url,
                retrieval_method=method, region_context=context.persisted,
                response_content_type=page.content_type,
                response_size=page.response_size or len(page.text.encode("utf-8")),
            )
        mapped_product_id = extract_product_id(mapped_url)
        if mapped_product_id is not None:
            candidates = [candidate for candidate in candidates
                          if str(candidate.get("product_id")) == mapped_product_id]
        result = self._result(page, candidates, comparable_key, region,
                              retrieval_method=method,
                              region_context=context.persisted)
        if not result.observations:
            return replace(
                result,
                health="degraded",
                error="NO RELIABLE MATCH" if candidates else "No Wildberries candidates returned",
            )
        return result
