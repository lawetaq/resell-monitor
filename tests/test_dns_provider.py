from __future__ import annotations

import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from src.retail import aggregate_retail_offers
from src.retail_monitor import RetailMonitor
from src.retail_providers.dns import (
    DnsRetailProvider, dns_product_path_id, is_dns_challenge, parse_dns,
    validate_dns_url,
)
from src.sources.http import HttpPage
from src.storage import ListingRepository

KEY = "gpu:rtx-3060:12gb"
FIXTURES = Path(__file__).parent / "fixtures"
PRODUCT_URL = ("https://www.dns-shop.ru/product/891c039318b0cb67/"
               "videokarta-afox-geforce-rtx-3060-af3060-12gd6h4-v4/")


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class DnsParserTests(unittest.TestCase):
    def test_product_json_ld_identity_price_and_availability(self) -> None:
        row = parse_dns(fixture("dns_product.html"))[0]
        self.assertEqual(row["price"], 31999)
        self.assertEqual(row["product_id"], "9279718")
        self.assertEqual(row["availability"], "available")
        self.assertEqual(row["url"], PRODUCT_URL)

    def test_search_card_normal_old_and_conditional_promo_price(self) -> None:
        rows = parse_dns(fixture("dns_search.html"))
        self.assertEqual(len(rows), 2)
        row = rows[0]
        self.assertEqual((row["price"], row["original_price"], row["conditional_price"]),
                         (31999, 35999, 30999))
        self.assertEqual(row["availability"], "available")

    def test_explicit_out_of_stock_and_unknown_are_distinct(self) -> None:
        source = fixture("dns_product.html")
        self.assertEqual(parse_dns(source.replace("InStock", "OutOfStock"))[0]["availability"],
                         "out_of_stock")
        self.assertEqual(parse_dns(source.replace("InStock", "PreOrder"))[0]["availability"],
                         "unknown")

    def test_canonical_mapping_validation_and_path_identity(self) -> None:
        self.assertEqual(validate_dns_url(PRODUCT_URL), PRODUCT_URL)
        self.assertEqual(dns_product_path_id(PRODUCT_URL), "891c039318b0cb67")
        for invalid in ("https://evil.test/product/891c039318b0cb67/x/",
                        "https://www.dns-shop.ru/search/?q=x",
                        "http://www.dns-shop.ru/product/891c039318b0cb67/x/"):
            with self.assertRaises(ValueError):
                validate_dns_url(invalid)

    def test_qrator_detection_is_content_based(self) -> None:
        self.assertTrue(is_dns_challenge(HttpPage("Qrator checking", 200, PRODUCT_URL, "text/html")))
        self.assertFalse(is_dns_challenge(HttpPage(fixture("dns_product.html"), 200,
                                                   PRODUCT_URL, "text/html")))


class DnsProviderTests(unittest.TestCase):
    def test_mapped_product_parsing_and_honest_region_scope(self) -> None:
        calls: list[str] = []
        def fetch(url: str) -> HttpPage:
            calls.append(url)
            return HttpPage(fixture("dns_product.html"), 200, url, "text/html")
        result = DnsRetailProvider(fetcher=fetch).search(
            KEY, "ignored", region="Khabarovsk", mapped_url=PRODUCT_URL)
        self.assertEqual(calls, [PRODUCT_URL])
        self.assertEqual(result.retrieval_method, "mapped_product")
        self.assertEqual(result.region_context, "Khabarovsk; dns_scope=default-unresolved")
        self.assertEqual(result.observations[0].product_id, "9279718")
        self.assertEqual(result.observations[0].price, 31999)

    def test_search_strictly_rejects_incompatible_product(self) -> None:
        result = DnsRetailProvider(fetcher=lambda _: HttpPage(
            fixture("dns_search.html"), 200, "https://www.dns-shop.ru/search/", "text/html",
        )).search(KEY, "RTX 3060 12GB")
        self.assertEqual(len(result.observations), 1)
        self.assertEqual(result.observations[0].product_id, "9279718")
        self.assertEqual(result.observations[0].conditional_price, 30999)

    def test_unknown_availability_cannot_drive_ranking(self) -> None:
        html = fixture("dns_product.html").replace("InStock", "PreOrder")
        result = DnsRetailProvider(fetcher=lambda _: HttpPage(
            html, 200, PRODUCT_URL, "text/html",
        )).search(KEY, "x", mapped_url=PRODUCT_URL)
        self.assertEqual(result.observations[0].match_confidence, "insufficient")
        self.assertIsNone(aggregate_retail_offers(list(result.observations)).representative_price)

    def test_qrator_401_and_http_200_are_blocked_before_parsing(self) -> None:
        for status in (401, 200):
            with self.subTest(status=status):
                result = DnsRetailProvider(fetcher=lambda _, s=status: HttpPage(
                    "<html>Qrator challenge</html>", s, PRODUCT_URL, "text/html",
                )).search(KEY, "x", mapped_url=PRODUCT_URL)
                self.assertEqual((result.health, result.block_classification),
                                 ("blocked", "challenge"))
                self.assertEqual(result.candidates_found, 0)

    def test_status_and_schema_classification(self) -> None:
        cases = ((401, "blocked", "access_blocked"), (403, "blocked", "access_blocked"),
                 (429, "blocked", "rate_limited"), (503, "degraded", "provider_error"))
        for status, health, classification in cases:
            with self.subTest(status=status):
                result = DnsRetailProvider(fetcher=lambda _, s=status: HttpPage(
                    "", s, PRODUCT_URL, "text/html",
                )).search(KEY, "x", mapped_url=PRODUCT_URL)
                self.assertEqual((result.health, result.block_classification),
                                 (health, classification))
        result = DnsRetailProvider(fetcher=lambda _: HttpPage(
            "<html>ordinary but changed</html>", 200, PRODUCT_URL, "text/html",
        )).search(KEY, "x", mapped_url=PRODUCT_URL)
        self.assertEqual((result.health, result.block_classification),
                         ("degraded", "schema_changed"))

    def test_explicit_block_is_one_provider_call_and_respects_retry_after(self) -> None:
        calls = 0
        def fetch(_: str) -> HttpPage:
            nonlocal calls
            calls += 1
            return HttpPage("Qrator", 401, PRODUCT_URL, "text/html", retry_after="7200")
        provider = DnsRetailProvider(fetcher=fetch)
        with tempfile.TemporaryDirectory() as root, ListingRepository(Path(root) / "x.db") as repo:
            refresh = RetailMonitor({"dns": provider}, repo,
                                    interval=timedelta(hours=1)).refresh(KEY, "x",
                                                                               mappings={"dns": PRODUCT_URL})
            state = repo.retail_provider_states()[0]
        self.assertEqual(calls, 1)
        self.assertEqual(refresh[0].block_classification, "challenge")
        self.assertEqual(state["retry_after_seconds"], 7200)

    def test_invalid_mapping_never_falls_back_to_search(self) -> None:
        calls: list[str] = []
        provider = DnsRetailProvider(fetcher=lambda url: calls.append(url))  # type: ignore[arg-type]
        result = provider.search(KEY, "RTX 3060", mapped_url="https://www.dns-shop.ru/search/")
        self.assertEqual(calls, [])
        self.assertEqual(result.block_classification, "invalid_mapping")


if __name__ == "__main__":
    unittest.main()
