from __future__ import annotations

import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

import requests

from src.retail import aggregate_retail_offers
from src.retail_monitor import RetailMonitor
from src.retail_providers.ozon import (
    OzonRetailProvider, parse_ozon, product_id_from_url, validate_ozon_url,
)
from src.sources.http import HttpPage, HttpTransport
from src.storage import ListingRepository

KEY = "gpu:rtx-3060:12gb"
FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class OzonParserTests(unittest.TestCase):
    def test_current_search_parsing_prices_sellers_and_incompatible_item(self) -> None:
        rows = parse_ozon(fixture("ozon_search.html"))
        self.assertEqual(len(rows), 3)
        compatible = [row for row in rows if row["product_id"] == "3551901878"]
        self.assertEqual([row["price"] for row in compatible], [31990, 32500])
        self.assertEqual(compatible[0]["conditional_price"], 29490)
        self.assertEqual(compatible[0]["original_price"], 42990)
        self.assertEqual(compatible[0]["seller"], "Computer Shop")
        self.assertEqual(compatible[0]["offer_id"], "offer-a")

    def test_mapped_product_parsing_identity_and_availability(self) -> None:
        row = parse_ozon(fixture("ozon_product.html"))[0]
        self.assertEqual(row["product_id"], "3551901878")
        self.assertEqual(row["availability"], "available")
        self.assertEqual(product_id_from_url(str(row["url"])), "3551901878")

    def test_unavailable_product_is_explicit_only(self) -> None:
        html = '<script type="application/json">{"title":"RTX 3060 12GB","sku":"1234567","price":"1 000 ₽","availability":"out_of_stock"}</script>'
        self.assertEqual(parse_ozon(html)[0]["availability"], "out_of_stock")
        unknown = html.replace('"availability":"out_of_stock"', '"note":"not in search"')
        self.assertEqual(parse_ozon(unknown)[0]["availability"], "unknown")

    def test_mapping_url_validation(self) -> None:
        self.assertEqual(validate_ozon_url("https://www.ozon.ru/product/gpu-3551901878/"),
                         "https://www.ozon.ru/product/gpu-3551901878/")
        for invalid in ("https://evil.test/product/gpu-3551901878/", "https://www.ozon.ru/search/"):
            with self.assertRaises(ValueError):
                validate_ozon_url(invalid)


class _Session:
    def __init__(self, responses: list[requests.Response]) -> None:
        self.responses = responses
        self.calls: list[str] = []
        self.proxies: dict[str, str] = {}

    def get(self, url: str, **_: object) -> requests.Response:
        self.calls.append(url)
        if not self.responses:
            raise AssertionError("unexpected retry")
        return self.responses.pop(0)

    def close(self) -> None: pass


def response(status: int, url: str, *, location: str | None = None,
             body: str = "", content_type: str = "text/html") -> requests.Response:
    result = requests.Response()
    result.status_code = status
    result.url = url
    result._content = body.encode()
    result.headers["content-type"] = content_type
    if location:
        result.headers["location"] = location
    return result


class OzonRedirectTests(unittest.TestCase):
    def test_bounded_307_then_403_preserves_chain_without_retry(self) -> None:
        first = response(307, "https://www.ozon.ru/search/?text=x",
                         location="/search/?text=x&__rr=1")
        first.cookies.set("__Secure-ETC", "private")
        session = _Session([first, response(403, "https://www.ozon.ru/search/?text=x&__rr=1")])
        page = HttpTransport(session=session).fetch_bounded(
            "https://www.ozon.ru/search/?text=x", accept_error_status=True)
        self.assertEqual(page.status_code, 403)
        self.assertEqual(len(session.calls), 2)
        self.assertEqual(page.redirect_chain[0].added_query_names, ("__rr",))
        self.assertEqual(page.redirect_chain[0].cookie_names, ("__Secure-ETC",))

    def test_redirect_loop_stops_before_repeated_request(self) -> None:
        session = _Session([response(307, "https://www.ozon.ru/a", location="/b"),
                            response(307, "https://www.ozon.ru/b", location="/a")])
        page = HttpTransport(session=session).fetch_bounded(
            "https://www.ozon.ru/a", accept_error_status=True)
        self.assertEqual(page.redirect_classification, "redirect_loop")
        self.assertEqual(len(session.calls), 2)


class OzonProviderTests(unittest.TestCase):
    def test_search_exact_matching_multiple_offers_and_region_scope(self) -> None:
        provider = OzonRetailProvider(fetcher=lambda _: HttpPage(
            fixture("ozon_search.html"), 200, "https://www.ozon.ru/search/", "text/html"))
        result = provider.search(KEY, "RTX 3060 12GB", region="Khabarovsk")
        self.assertEqual(len(result.observations), 2)
        self.assertEqual(result.region_context, "Khabarovsk; ozon_scope=default-unresolved")
        self.assertEqual(aggregate_retail_offers(list(result.observations)).representative_price, 32245)
        self.assertTrue(all(row.conditional_price < row.price for row in result.observations))

    def test_mapped_product_filters_wrong_sku_and_does_not_search_fallback(self) -> None:
        calls: list[str] = []
        def fetch(url: str) -> HttpPage:
            calls.append(url)
            return HttpPage(fixture("ozon_product.html"), 200, url, "text/html")
        provider = OzonRetailProvider(fetcher=fetch)
        result = provider.search(KEY, "ignored", mapped_url="https://www.ozon.ru/product/wrong-9999999999/")
        self.assertEqual(result.observations, ())
        self.assertEqual(result.block_classification, "mapping_identity_mismatch")
        self.assertEqual(calls, ["https://www.ozon.ru/product/wrong-9999999999/"])

    def test_unknown_availability_cannot_drive_retail_ranking(self) -> None:
        html = '<script type="application/json">{"title":"RTX 3060 12GB","sku":"1234567","price":"1 000 ₽"}</script>'
        result = OzonRetailProvider(fetcher=lambda _: HttpPage(
            html, 200, "https://www.ozon.ru/product/gpu-1234567/", "text/html",
        )).search(KEY, "x", mapped_url="https://www.ozon.ru/product/gpu-1234567/")
        self.assertEqual(result.observations[0].match_confidence, "insufficient")
        self.assertIsNone(aggregate_retail_offers(list(result.observations)).representative_price)

    def test_status_and_schema_classification(self) -> None:
        cases = ((401, "blocked", "access_blocked"), (403, "blocked", "access_blocked"),
                 (429, "blocked", "rate_limited"), (503, "degraded", "provider_error"))
        for status, health, classification in cases:
            with self.subTest(status=status):
                provider = OzonRetailProvider(fetcher=lambda _, s=status: HttpPage("", s, "https://www.ozon.ru/", "text/html"))
                result = provider.search(KEY, "RTX 3060 12GB")
                self.assertEqual((result.health, result.block_classification), (health, classification))
        result = OzonRetailProvider(fetcher=lambda _: HttpPage("<html></html>", 200, "https://www.ozon.ru/", "text/html")).search(KEY, "x")
        self.assertEqual((result.health, result.block_classification), ("degraded", "schema_changed"))

    def test_429_cooldown_and_no_provider_retry(self) -> None:
        calls = 0
        def fetch(_: str) -> HttpPage:
            nonlocal calls
            calls += 1
            return HttpPage("", 429, "https://www.ozon.ru/search/", "text/html", retry_after="7200")
        provider = OzonRetailProvider(fetcher=fetch)
        with tempfile.TemporaryDirectory() as root, ListingRepository(Path(root) / "x.db") as repo:
            rows = RetailMonitor({"ozon": provider}, repo, interval=timedelta(hours=1)).refresh(KEY, "x")
            state = repo.retail_provider_states()[0]
        self.assertEqual(calls, 1)
        self.assertEqual(rows[0].health, "blocked")
        self.assertEqual(state["retry_after_seconds"], 7200)


if __name__ == "__main__":
    unittest.main()
