from __future__ import annotations

import json
import re

from bs4 import BeautifulSoup

from src.retail import RetailRetrievalResult
from .common import HttpRetailProvider


def parse_dns(html: str, base_url: str = "https://www.dns-shop.ru") -> list[dict[str, object]]:
    soup = BeautifulSoup(html, "html.parser")
    found: list[dict[str, object]] = []
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            values = json.loads(script.string or "null")
        except (ValueError, TypeError):
            continue
        for value in values if isinstance(values, list) else [values]:
            if not isinstance(value, dict) or value.get("@type") != "Product":
                continue
            offers = value.get("offers") or {}
            offer_list = offers if isinstance(offers, list) else [offers]
            for offer in offer_list:
                if isinstance(offer, dict) and offer.get("price"):
                    found.append({"title": value.get("name"), "price": offer["price"],
                                  "url": value.get("url") or offer.get("url"),
                                  "availability": "available" if "InStock" in str(offer.get("availability")) else "unknown",
                                  "product_id": value.get("sku")})
    for card in soup.select("[data-product-card], .catalog-product"):
        title_node = card.select_one("[data-role='title'], .catalog-product__name")
        price_node = card.select_one("[data-price], .product-buy__price")
        if not title_node or not price_node:
            continue
        price_text = price_node.get("data-price") or price_node.get_text(" ", strip=True)
        digits = re.sub(r"\D", "", str(price_text))
        link = card.select_one("a[href]")
        if digits:
            found.append({"title": title_node.get_text(" ", strip=True), "price": int(digits),
                          "url": link.get("href") if link else None,
                          "product_id": card.get("data-product-id")})
    return _unique(found)


class DnsRetailProvider(HttpRetailProvider):
    name = "dns"
    search_url = "https://www.dns-shop.ru/search/?q={query}"

    def search(self, comparable_key: str, query: str, *, region: str | None = None,
               mapped_url: str | None = None) -> RetailRetrievalResult:
        page = self._fetcher(self._url(query, region, mapped_url))
        return self._result(page, parse_dns(page.text, page.final_url), comparable_key, region)


def _unique(items: list[dict[str, object]]) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    seen: set[tuple[object, object]] = set()
    for item in items:
        key = (item.get("product_id") or item.get("url"), item.get("price"))
        if key not in seen:
            seen.add(key); result.append(item)
    return result
