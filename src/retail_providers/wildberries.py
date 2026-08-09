from __future__ import annotations

import json

from src.retail import RetailRetrievalResult
from .common import HttpRetailProvider


def parse_wildberries(body: str) -> list[dict[str, object]]:
    value = json.loads(body)
    products = value.get("data", {}).get("products", [])
    result: list[dict[str, object]] = []
    for product in products:
        price = product.get("salePriceU")
        sizes = product.get("sizes") or []
        if sizes:
            price_data = sizes[0].get("price") or {}
            price = price_data.get("product") or price_data.get("basic") or price
        if not price:
            continue
        result.append({"title": product.get("name"), "price": int(price) // 100,
            "original_price": int(product.get("priceU", 0)) // 100 or None,
            "url": f"https://www.wildberries.ru/catalog/{product.get('id')}/detail.aspx",
            "product_id": product.get("id"), "offer_id": product.get("id"),
            "seller": product.get("supplier") or product.get("supplierName"),
            "availability": "available" if int(product.get("totalQuantity", 1)) > 0 else "out_of_stock"})
    return result


class WildberriesRetailProvider(HttpRetailProvider):
    name = "wildberries"
    search_url = ("https://search.wb.ru/exactmatch/ru/common/v18/search?"
                  "ab_testing=false&appType=1&curr=rub&dest={region}&query={query}&resultset=catalog")

    def _url(self, query: str, region: str | None, mapped_url: str | None) -> str:
        return super()._url(query, region or "-1221185", mapped_url)

    def search(self, comparable_key: str, query: str, *, region: str | None = None,
               mapped_url: str | None = None) -> RetailRetrievalResult:
        page = self._fetcher(self._url(query, region, mapped_url))
        return self._result(page, parse_wildberries(page.text), comparable_key, region)
