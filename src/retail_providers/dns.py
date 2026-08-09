from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import replace
from urllib.parse import quote_plus, urljoin, urlsplit

from bs4 import BeautifulSoup

from src.retail import RetailRetrievalResult
from src.sources.http import HttpPage, HttpTransport, HttpTransportError

from .common import Fetcher, HttpRetailProvider, parse_retry_after

DNS_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.7",
}
_PRODUCT_PATH = re.compile(r"^/product/([0-9a-f]{16})/[^/]+/?$", re.I)
_CHALLENGE_MARKERS = (
    "qrator", "checking your browser", "проверяем, что вы не робот",
    "пройдите проверку", "captcha", "access denied",
)


def parse_dns(html: str, base_url: str = "https://www.dns-shop.ru") -> list[dict[str, object]]:
    """Parse explicit DNS product evidence from JSON-LD and catalog cards."""
    soup = BeautifulSoup(html, "html.parser")
    found: list[dict[str, object]] = []
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            root = json.loads(script.string or "null")
        except (ValueError, TypeError):
            continue
        for value in _json_objects(root):
            types = value.get("@type")
            if not (types == "Product" or isinstance(types, list) and "Product" in types):
                continue
            offers = value.get("offers") or {}
            for offer in offers if isinstance(offers, list) else [offers]:
                if not isinstance(offer, dict):
                    continue
                price = _price(offer.get("price"))
                if price is None:
                    continue
                raw_url = value.get("url") or offer.get("url")
                found.append({
                    "title": value.get("name"), "price": price,
                    "url": urljoin(base_url, str(raw_url)) if raw_url else None,
                    "availability": _availability(offer.get("availability")),
                    "product_id": value.get("sku") or value.get("productID"),
                })
    for card in soup.select("[data-product-card], .catalog-product"):
        title_node = card.select_one("[data-role='title'], .catalog-product__name")
        price_node = card.select_one("[data-price], .product-buy__price")
        if not title_node or not price_node:
            continue
        price = _price(price_node.get("data-price") or price_node.get_text(" ", strip=True))
        if price is None:
            continue
        link = card.select_one("a[href]")
        raw_url = link.get("href") if link else None
        found.append({
            "title": title_node.get_text(" ", strip=True), "price": price,
            "original_price": _price(card.get("data-old-price")),
            # A lower explicitly labelled promotion is conditional until its
            # eligibility terms can be established from source data.
            "conditional_price": _price(card.get("data-promo-price")),
            "url": urljoin(base_url, str(raw_url)) if raw_url else None,
            "product_id": card.get("data-product-id"),
            "availability": _availability(card.get("data-availability")),
        })
    return _unique(found)


def _json_objects(value: object) -> Iterator[dict[str, object]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _json_objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from _json_objects(child)


def _price(value: object) -> int | None:
    if value is None or isinstance(value, (dict, list, bool)):
        return None
    digits = "".join(character for character in str(value).split(",", 1)[0] if character.isdigit())
    return int(digits) if digits else None


def _availability(value: object) -> str:
    normalized = str(value or "").casefold().replace("_", "")
    if any(marker in normalized for marker in ("outofstock", "soldout", "unavailable")):
        return "out_of_stock"
    if "instock" in normalized or normalized == "available":
        return "available"
    return "unknown"


def dns_product_path_id(url: str | None) -> str | None:
    if not url:
        return None
    match = _PRODUCT_PATH.fullmatch(urlsplit(url).path)
    return match.group(1).casefold() if match else None


def validate_dns_url(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.hostname not in {"dns-shop.ru", "www.dns-shop.ru"}:
        raise ValueError("mapped DNS URL must use https://www.dns-shop.ru")
    if dns_product_path_id(url) is None:
        raise ValueError("mapped DNS URL must be a canonical /product/<id>/<slug>/ URL")
    return url


def is_dns_challenge(page: HttpPage) -> bool:
    body = page.text.casefold()
    return any(marker in body for marker in _CHALLENGE_MARKERS)


class DnsRetailProvider(HttpRetailProvider):
    name = "dns"
    search_url = "https://www.dns-shop.ru/search/?q={query}"

    def __init__(self, *, transport: HttpTransport | None = None,
                 fetcher: Fetcher | None = None) -> None:
        if fetcher is not None:
            super().__init__(transport=transport, fetcher=fetcher)
            return
        self._transport = transport or HttpTransport(headers=DNS_HEADERS)
        self._fetcher = lambda url: self._transport.fetch(url, accept_error_status=True)

    def search(self, comparable_key: str, query: str, *, region: str | None = None,
               mapped_url: str | None = None) -> RetailRetrievalResult:
        method = "mapped_product" if mapped_url else "search"
        try:
            url = validate_dns_url(mapped_url) if mapped_url else self.search_url.format(query=quote_plus(query))
        except ValueError as error:
            return RetailRetrievalResult(
                self.name, (), transport="requests-persistent", health="degraded",
                error=str(error), retrieval_method=method,
                region_context=_region_context(region), block_classification="invalid_mapping")
        try:
            page = self._fetcher(url)
        except HttpTransportError as error:
            return RetailRetrievalResult(
                self.name, (), transport="requests-persistent", health="failed",
                error=str(error), retrieval_method=method,
                region_context=_region_context(region), block_classification="network_error")
        challenge = is_dns_challenge(page)
        candidates = parse_dns(page.text, page.final_url) if page.status_code == 200 and not challenge else []
        expected_path_id = dns_product_path_id(mapped_url) if mapped_url else None
        mapping_mismatch = False
        if expected_path_id and candidates:
            identified = [row for row in candidates if dns_product_path_id(str(row.get("url") or ""))]
            if identified:
                candidates = [row for row in candidates
                              if dns_product_path_id(str(row.get("url") or "")) == expected_path_id]
                mapping_mismatch = not candidates
        common = self._result(page, candidates, comparable_key, region,
                              retrieval_method=method, region_context=_region_context(region))
        observations = tuple(
            replace(item, match_confidence="insufficient")
            if item.availability == "unknown" else item
            for item in common.observations
        )
        health, classification, error = _classify(page, bool(candidates), challenge,
                                                   mapping_mismatch)
        return RetailRetrievalResult(
            common.retailer, observations, common.status_code, "requests-persistent",
            health, error, common.candidates_found, common.raw_body, common.final_url,
            method, common.region_context, parse_retry_after(page.retry_after),
            classification, common.response_content_type, common.response_size,
            page.redirect_chain, page.redirect_classification,
        )


def _region_context(region: str | None) -> str:
    return f"{region}; dns_scope=default-unresolved" if region else "dns_scope=default-unresolved"


def _classify(page: HttpPage, parsed: bool, challenge: bool,
              mapping_mismatch: bool = False) -> tuple[str, str, str | None]:
    if challenge:
        return "blocked", "challenge", f"HTTP {page.status_code} DNS/Qrator challenge"
    if page.status_code in {401, 403}:
        return "blocked", "access_blocked", f"HTTP {page.status_code}"
    if page.status_code == 429:
        return "blocked", "rate_limited", "HTTP 429"
    if 500 <= page.status_code < 600:
        return "degraded", "provider_error", f"HTTP {page.status_code}"
    if page.status_code != 200:
        return "degraded", "http_error", f"HTTP {page.status_code}"
    if mapping_mismatch:
        return "degraded", "mapping_identity_mismatch", "mapped DNS path ID did not match returned product"
    if not parsed:
        return "degraded", "schema_changed", "HTTP 200 contained no recognized DNS product data"
    return "healthy", "none", None


def _unique(items: list[dict[str, object]]) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    seen: set[tuple[object, object]] = set()
    for item in items:
        key = (item.get("product_id") or item.get("url"), item.get("price"))
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result
