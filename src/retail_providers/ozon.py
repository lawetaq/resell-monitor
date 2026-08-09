from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import replace
from urllib.parse import quote_plus, urlsplit

from bs4 import BeautifulSoup

from src.retail import RetailRetrievalResult
from src.sources.http import HttpPage, HttpTransport, HttpTransportError
from .common import Fetcher, HttpRetailProvider, parse_retry_after

OZON_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.7",
}
_PRODUCT_ID = re.compile(r"-(\d{6,})(?:/|$)")


def parse_ozon(html: str) -> list[dict[str, object]]:
    """Parse Ozon's embedded widget/state JSON without executing JavaScript."""
    soup = BeautifulSoup(html, "html.parser")
    roots: list[object] = []
    for script in soup.select("script"):
        text = script.string or script.get_text()
        if not text or not text.lstrip().startswith(("{", "[")):
            continue
        try:
            roots.append(json.loads(text))
        except ValueError:
            continue
    candidates: list[dict[str, object]] = []
    for root in roots:
        for item in _walk(root):
            candidate = _candidate(item)
            if candidate:
                candidates.append(candidate)
    return _dedupe(candidates)


def _candidate(item: dict[str, object]) -> dict[str, object] | None:
    title = _first(item, "title", "name")
    if not title:
        return None
    price_nodes = [item]
    for key in ("price", "priceInfo", "offer", "offers", "mainState"):
        value = item.get(key)
        if isinstance(value, dict):
            price_nodes.append(value)
        elif isinstance(value, list):
            price_nodes.extend(x for x in _walk(value))
    normal = _named_price(price_nodes, ("price", "finalPrice", "salePrice", "currentPrice", "priceWithoutCard"))
    conditional = _named_price(price_nodes, ("cardPrice", "ozonCardPrice", "priceWithCard", "bankPrice"))
    original = _named_price(price_nodes, ("originalPrice", "oldPrice", "listPrice"))
    # Current cards commonly expose cardPrice (conditional) alongside price
    # (ordinary payment). Never promote the lower conditional price.
    if normal is None:
        return None
    link = _link(item)
    product_id = _text(_first(item, "sku", "productId", "id")) or product_id_from_url(link)
    seller = item.get("seller")
    seller_name = (_first(seller, "name", "title") if isinstance(seller, dict)
                   else _first(item, "sellerName", "sellerTitle"))
    seller_id = (_first(seller, "id", "sellerId") if isinstance(seller, dict)
                 else _first(item, "sellerId"))
    offer_id = _first(item, "offerId", "sellerOfferId")
    availability = _availability(item)
    return {
        "title": str(title), "price": normal, "original_price": original,
        "conditional_price": conditional, "conditional_price_type": "ozon_card_or_bank" if conditional else None,
        "url": link, "product_id": product_id, "offer_id": _text(offer_id) or _text(seller_id),
        "seller": _text(seller_name), "availability": availability,
    }


def _walk(value: object) -> Iterator[dict[str, object]]:
    if isinstance(value, str) and value.lstrip().startswith(("{", "[")):
        try:
            value = json.loads(value)
        except ValueError:
            return
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _named_price(nodes: list[dict[str, object]], names: tuple[str, ...]) -> int | None:
    for node in nodes:
        for name in names:
            if name in node:
                parsed = _price(node[name])
                if parsed is not None:
                    return parsed
    return None


def _price(value: object) -> int | None:
    if value is None or isinstance(value, (dict, list, bool)):
        return None
    digits = "".join(ch for ch in str(value).split(",", 1)[0] if ch.isdigit())
    return int(digits) if digits else None


def _first(value: object, *names: str) -> object | None:
    if not isinstance(value, dict):
        return None
    return next((value[name] for name in names if value.get(name) not in (None, "")), None)


def _text(value: object) -> str | None:
    return str(value) if value not in (None, "") else None


def _link(item: dict[str, object]) -> str | None:
    direct = _first(item, "link", "url")
    if direct:
        return str(direct)
    action = item.get("action")
    return _text(_first(action, "link", "url"))


def _availability(item: dict[str, object]) -> str:
    value = str(_first(item, "availability", "stockStatus", "state") or "").casefold()
    if value in {"out_of_stock", "outofstock", "unavailable", "soldout"}:
        return "out_of_stock"
    if value in {"in_stock", "instock", "available"}:
        return "available"
    quantity = _first(item, "quantity", "stock", "availableQuantity")
    if isinstance(quantity, (int, float)):
        return "available" if quantity > 0 else "out_of_stock"
    return "unknown"


def _dedupe(items: list[dict[str, object]]) -> list[dict[str, object]]:
    keyed: dict[tuple[str, str, int], dict[str, object]] = {}
    for item in items:
        identity = str(item.get("offer_id") or item.get("product_id") or item.get("url") or item["title"])
        keyed[(identity, str(item.get("seller") or ""), int(item["price"]))] = item
    return list(keyed.values())


def product_id_from_url(url: str | None) -> str | None:
    if not url:
        return None
    match = _PRODUCT_ID.search(urlsplit(url).path)
    return match.group(1) if match else None


def validate_ozon_url(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.hostname not in {"ozon.ru", "www.ozon.ru"}:
        raise ValueError("mapped Ozon URL must use https://www.ozon.ru or https://ozon.ru")
    if not product_id_from_url(url):
        raise ValueError("mapped Ozon product URL must contain a numeric product ID")
    return url


class OzonRetailProvider(HttpRetailProvider):
    name = "ozon"
    search_url = "https://www.ozon.ru/search/?text={query}&from_global=true"

    def __init__(self, *, transport: HttpTransport | None = None,
                 fetcher: Fetcher | None = None) -> None:
        if fetcher is not None:
            super().__init__(transport=transport, fetcher=fetcher)
            return
        self._transport = transport or HttpTransport(headers=OZON_HEADERS)
        self._fetcher = lambda url: self._transport.fetch_bounded(
            url, accept_error_status=True, max_redirects=3)

    def search(self, comparable_key: str, query: str, *, region: str | None = None,
               mapped_url: str | None = None) -> RetailRetrievalResult:
        method = "mapped_product" if mapped_url else "search"
        try:
            url = validate_ozon_url(mapped_url) if mapped_url else self.search_url.format(query=quote_plus(query))
            page = self._fetcher(url)
        except (HttpTransportError, ValueError) as error:
            return RetailRetrievalResult(
                self.name, (), transport="requests-persistent-bounded", health="failed",
                error=str(error), retrieval_method=method,
                region_context=_region_context(region), block_classification="network_error")
        parsed_candidates = parse_ozon(page.text) if page.status_code == 200 else []
        candidates = parsed_candidates
        expected_id = product_id_from_url(mapped_url) if mapped_url else None
        if expected_id:
            candidates = [row for row in candidates if str(row.get("product_id") or "") == expected_id]
        result = self._result(page, candidates, comparable_key, region,
                              retrieval_method=method, region_context=_region_context(region))
        observations = tuple(
            replace(row, match_confidence="insufficient")
            if row.availability == "unknown" else row
            for row in result.observations
        )
        mapping_mismatch = bool(expected_id and parsed_candidates and not candidates)
        health, classification, error = _classify(page, bool(candidates), mapping_mismatch)
        return RetailRetrievalResult(
            result.retailer, observations, result.status_code,
            "requests-persistent-bounded", health, error, result.candidates_found,
            result.raw_body, result.final_url, method, result.region_context,
            parse_retry_after(page.retry_after), classification,
            result.response_content_type, result.response_size,
            page.redirect_chain, page.redirect_classification,
        )


def _region_context(region: str | None) -> str:
    return f"{region}; ozon_scope=default-unresolved" if region else "ozon_scope=default-unresolved"


def _classify(page: HttpPage, parsed: bool,
              mapping_mismatch: bool = False) -> tuple[str, str, str | None]:
    if page.redirect_classification in {"redirect_loop", "redirect_limit"}:
        return "degraded", page.redirect_classification, page.redirect_classification
    if page.status_code in {401, 403}:
        return "blocked", "access_blocked", f"HTTP {page.status_code}"
    if page.status_code == 429:
        return "blocked", "rate_limited", "HTTP 429"
    if 500 <= page.status_code < 600:
        return "degraded", "provider_error", f"HTTP {page.status_code}"
    if page.status_code != 200:
        return "degraded", "http_error", f"HTTP {page.status_code}"
    if mapping_mismatch:
        return "degraded", "mapping_identity_mismatch", "mapped product ID did not match the returned product"
    if not parsed:
        return "degraded", "schema_changed", "HTTP 200 contained no recognized Ozon product data"
    return "healthy", "none", None
