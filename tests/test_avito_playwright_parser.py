from __future__ import annotations

import unittest
from pathlib import Path

from bs4 import BeautifulSoup

from src.sources.avito_playwright_parser import (
    PlaywrightExtractionError,
    extract_dom_listings,
    extract_network_items,
)
from src.sources.avito_transport import (
    is_avito_owned_url,
    is_json_content_type,
    safe_response_url,
    sanitize_json,
)

FIXTURE = Path(__file__).resolve().parents[1] / "output" / "avito_response.html"


class PlaywrightListingParserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = FIXTURE.read_text(encoding="utf-8")

    def test_extracts_catalog_items_from_network_payload(self) -> None:
        item = {"id": 1, "title": "One", "urlPath": "/item_1"}

        result = extract_network_items([{"catalog": {"items": [item]}}])

        self.assertEqual(result, [item])

    def test_extracts_nested_result_catalog(self) -> None:
        item = {"id": 2, "title": "Two", "urlPath": "/item_2"}

        result = extract_network_items(
            [{"result": {"catalog": {"items": [item]}}}]
        )

        self.assertEqual(result, [item])

    def test_ignores_unrelated_json_items(self) -> None:
        payloads = [
            {"items": [{"event": "view"}]},
            {"metrics": {"items": [1, 2, 3]}},
        ]

        self.assertIsNone(extract_network_items(payloads))

    def test_extracts_all_dom_listings_from_fixture(self) -> None:
        listings = extract_dom_listings(self.html)

        self.assertEqual(len(listings), 50)
        self.assertEqual(listings[0].external_id, "8277979445")
        self.assertEqual(listings[0].title, "Оперативная память ddr4 16gb")
        self.assertEqual(listings[0].price, 5000)
        self.assertEqual(listings[0].price_display, "5\xa0000 ₽")
        self.assertIsNone(listings[0].location)
        self.assertTrue(listings[0].url.startswith("https://www.avito.ru/"))
        self.assertEqual(listings[1].location, "р-н Индустриальный")

    def test_dom_extraction_deduplicates_external_ids(self) -> None:
        soup = BeautifulSoup(self.html, "html.parser")
        card = soup.select_one('[data-marker="item"]')
        assert card is not None
        duplicated = f"<html><body>{card}{card}</body></html>"

        listings = extract_dom_listings(duplicated)

        self.assertEqual(len(listings), 1)

    def test_dom_extraction_rejects_missing_required_fields(self) -> None:
        malformed = '<div data-marker="item" data-item-id="1"></div>'

        with self.assertRaisesRegex(PlaywrightExtractionError, "missing its URL"):
            extract_dom_listings(malformed)

    def test_dom_extraction_rejects_page_without_cards(self) -> None:
        with self.assertRaisesRegex(PlaywrightExtractionError, "were not found"):
            extract_dom_listings("<html><body>ordinary page</body></html>")

    def test_network_capture_filters_hosts_and_content_types(self) -> None:
        self.assertTrue(is_avito_owned_url("https://www.avito.ru/web/search?q=x"))
        self.assertTrue(is_avito_owned_url("https://www.avito.st/data.json"))
        self.assertFalse(is_avito_owned_url("https://example.com/data.json"))
        self.assertTrue(is_json_content_type("application/json; charset=utf-8"))
        self.assertTrue(is_json_content_type("application/problem+json"))
        self.assertFalse(is_json_content_type("text/html"))

    def test_network_diagnostics_drop_query_and_sensitive_fields(self) -> None:
        self.assertEqual(
            safe_response_url("https://www.avito.ru/web/search?token=secret#part"),
            "https://www.avito.ru/web/search",
        )
        sanitized = sanitize_json(
            {
                "catalog": {"items": []},
                "accessToken": "secret",
                "session": {"id": "secret"},
                "nested": {"cookieValue": "secret", "safe": 1},
            }
        )

        self.assertEqual(
            sanitized,
            {"catalog": {"items": []}, "nested": {"safe": 1}},
        )


if __name__ == "__main__":
    unittest.main()
