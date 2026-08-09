from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from src.retail_monitor import RetailMonitor
from src.retail_providers.wildberries import (
    CARD_ENDPOINT,
    SEARCH_ENDPOINT,
    WildberriesRetailProvider,
    build_wildberries_url,
    parse_wildberries,
    resolve_wildberries_region,
)
from src.sources.http import HttpPage
from src.sources.http import HttpTransportError
from src.storage import ListingRepository


KEY = "gpu:rtx-3060:12gb"
FIXTURES = Path(__file__).with_name("fixtures")


class WildberriesParserTests(unittest.TestCase):
    def test_current_search_prices_variants_availability_and_conditional_price(self) -> None:
        rows = parse_wildberries((FIXTURES / "wildberries_search.json").read_text())
        exact = [row for row in rows if row["product_id"] == 101]
        self.assertEqual([row["price"] for row in exact], [25_000, 25_500])
        self.assertEqual([row["conditional_price"] for row in exact], [23_000, 23_500])
        self.assertEqual(exact[0]["original_price"], 30_000)
        self.assertEqual(len({row["offer_id"] for row in exact}), 2)
        unavailable = next(row for row in rows if row["product_id"] == 103)
        self.assertEqual(unavailable["availability"], "out_of_stock")

    def test_current_mapped_card_response(self) -> None:
        rows = parse_wildberries((FIXTURES / "wildberries_card.json").read_text())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["product_id"], 101)
        self.assertEqual(rows[0]["seller"], "Example Seller")

    def test_region_requires_explicit_destination_context(self) -> None:
        unresolved = resolve_wildberries_region("Khabarovsk")
        self.assertIsNone(unresolved.destination)
        self.assertIn("wb_dest=unresolved", unresolved.persisted)
        resolved = resolve_wildberries_region("Khabarovsk; wb_dest=-1221185")
        self.assertEqual(resolved.destination, "-1221185")


class WildberriesProviderTests(unittest.TestCase):
    def page(self, body: str, url: str, *, status: int = 200,
             content_type: str = "application/json", retry_after: str | None = None) -> HttpPage:
        return HttpPage(body, status, url, content_type, len(body.encode()), retry_after)

    def test_search_uses_one_request_and_rejects_incompatible_product(self) -> None:
        calls: list[str] = []
        body = (FIXTURES / "wildberries_search.json").read_text()
        provider = WildberriesRetailProvider(fetcher=lambda url: calls.append(url) or self.page(body, url))
        result = provider.search(KEY, "RTX 3060 12GB", region="Khabarovsk")
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0].startswith(SEARCH_ENDPOINT))
        self.assertNotIn("dest", parse_qs(urlsplit(calls[0]).query))
        self.assertEqual({item.product_id for item in result.observations}, {"101", "103"})
        self.assertNotIn("102", {item.product_id for item in result.observations})
        self.assertEqual(result.retrieval_method, "search")
        self.assertIn("wb_dest=unresolved", result.region_context or "")

    def test_mapped_product_prefers_card_endpoint_and_exact_nm_id(self) -> None:
        calls: list[str] = []
        body = (FIXTURES / "wildberries_card.json").read_text()
        provider = WildberriesRetailProvider(fetcher=lambda url: calls.append(url) or self.page(body, url))
        result = provider.search(
            KEY, "RTX 3060 12GB", region="Khabarovsk; wb_dest=-1221185",
            mapped_url="https://www.wildberries.ru/catalog/101/detail.aspx",
        )
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0].startswith(CARD_ENDPOINT))
        parameters = parse_qs(urlsplit(calls[0]).query)
        self.assertEqual(parameters["nm"], ["101"])
        self.assertEqual(parameters["dest"], ["-1221185"])
        self.assertEqual(result.retrieval_method, "mapped_product")
        self.assertEqual(result.observations[0].conditional_price, 23_000)

    def test_invalid_mapping_never_falls_back_to_broad_search(self) -> None:
        calls: list[str] = []
        provider = WildberriesRetailProvider(fetcher=lambda url: calls.append(url))
        with self.assertRaisesRegex(ValueError, "numeric catalog nmId"):
            provider.search(KEY, "RTX 3060 12GB", mapped_url="https://example.test/item")
        self.assertEqual(calls, [])

    def test_success_persists_normal_and_conditional_price_separately(self) -> None:
        body = (FIXTURES / "wildberries_card.json").read_text()
        provider = WildberriesRetailProvider(fetcher=lambda url: self.page(body, url))
        with tempfile.TemporaryDirectory() as root, ListingRepository(Path(root) / "wb.db") as repository:
            RetailMonitor({"wildberries": provider}, repository).refresh(
                KEY, "RTX 3060 12GB", mappings={"wildberries": "101"}
            )
            row = repository.retail_observations(KEY)[0]
            self.assertEqual(row["price"], 25_000)
            self.assertEqual(row["conditional_price"], 23_000)

    def test_429_is_blocked_without_parse_or_retry(self) -> None:
        calls: list[str] = []
        provider = WildberriesRetailProvider(fetcher=lambda url: calls.append(url) or self.page(
            "rate limited", url, status=429, content_type="text/plain", retry_after="3600"
        ))
        result = provider.search(KEY, "RTX 3060 12GB", region="Khabarovsk")
        self.assertEqual(len(calls), 1)
        self.assertEqual(result.health, "blocked")
        self.assertEqual(result.block_classification, "rate_limited")
        self.assertEqual(result.retry_after_seconds, 3600)

    def test_network_failure_is_structured_without_retry(self) -> None:
        calls: list[str] = []
        def fail(url: str) -> HttpPage:
            calls.append(url)
            raise HttpTransportError("DNS unavailable", retryable=True)
        result = WildberriesRetailProvider(fetcher=fail).search(
            KEY, "RTX 3060 12GB", region="Khabarovsk"
        )
        self.assertEqual(len(calls), 1)
        self.assertEqual(result.health, "failed")
        self.assertEqual(result.block_classification, "network_error")

    def test_monitor_persists_block_context_and_retry_after_cooldown(self) -> None:
        now = datetime.now(timezone.utc)
        provider = WildberriesRetailProvider(fetcher=lambda url: self.page(
            "rate limited", url, status=429, content_type="text/plain", retry_after="3600"
        ))
        with tempfile.TemporaryDirectory() as root, ListingRepository(Path(root) / "wb.db") as repository:
            refresh = RetailMonitor(
                {"wildberries": provider}, repository, interval=timedelta(minutes=10)
            ).refresh(KEY, "RTX 3060 12GB", region="Khabarovsk")[0]
            state = repository.retail_provider_states()[0]
            self.assertEqual(refresh.health, "blocked")
            self.assertEqual(state["block_classification"], "rate_limited")
            self.assertEqual(state["retrieval_method"], "search")
            next_refresh = datetime.fromisoformat(state["next_refresh_at"])
            self.assertGreaterEqual(next_refresh, now + timedelta(minutes=59))


if __name__ == "__main__":
    unittest.main()
