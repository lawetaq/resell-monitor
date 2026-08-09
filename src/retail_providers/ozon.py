from __future__ import annotations

import json
from collections.abc import Iterator

from bs4 import BeautifulSoup

from src.retail import RetailRetrievalResult
from .common import HttpRetailProvider


def parse_ozon(html: str) -> list[dict[str, object]]:
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
            title = item.get("title") or item.get("name")
            price = _price(item.get("price") or item.get("finalPrice") or item.get("salePrice"))
            if title and price:
                candidates.append({"title": title, "price": price,
                    "original_price": _price(item.get("originalPrice") or item.get("oldPrice")),
                    "url": item.get("link") or item.get("url"),
                    "product_id": item.get("sku") or item.get("id"),
                    "offer_id": item.get("offerId"), "seller": item.get("sellerName"),
                    "availability": "available"})
    return _dedupe(candidates)


def _walk(value: object) -> Iterator[dict[str, object]]:
    if isinstance(value, str) and value.lstrip().startswith(("{", "[")):
        try: value = json.loads(value)
        except ValueError: return
    if isinstance(value, dict):
        yield value
        for child in value.values(): yield from _walk(child)
    elif isinstance(value, list):
        for child in value: yield from _walk(child)


def _price(value: object) -> int | None:
    if value is None: return None
    digits = "".join(ch for ch in str(value).split(",", 1)[0] if ch.isdigit())
    return int(digits) if digits else None


def _dedupe(items: list[dict[str, object]]) -> list[dict[str, object]]:
    return list({(str(x.get("product_id") or x.get("url") or x.get("title")), int(x["price"])): x for x in items}.values())


class OzonRetailProvider(HttpRetailProvider):
    name = "ozon"
    search_url = "https://www.ozon.ru/search/?text={query}&from_global=true"

    def search(self, comparable_key: str, query: str, *, region: str | None = None,
               mapped_url: str | None = None) -> RetailRetrievalResult:
        page = self._fetcher(self._url(query, region, mapped_url))
        return self._result(page, parse_ozon(page.text), comparable_key, region)
