from __future__ import annotations

import unittest
from pathlib import Path

from src.sources.avito import (
    AvitoError,
    extract_items,
    extract_loader_data,
    normalize_listing,
)

FIXTURE = (
    Path(__file__).resolve().parents[1] / "output" / "avito_response.html"
)


class AvitoParserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = FIXTURE.read_text(encoding="utf-8")
        cls.loader_data = extract_loader_data(cls.html)

    def test_extracts_loader_data_from_fixture(self) -> None:
        self.assertIn("catalog", self.loader_data)
        self.assertEqual(self.loader_data["totalCount"], 1518)

    def test_extracts_listings_from_fixture(self) -> None:
        items = extract_items(self.loader_data)

        self.assertEqual(len(items), 50)
        self.assertTrue(all(isinstance(item, dict) for item in items))

    def test_normalizes_listing(self) -> None:
        item = extract_items(self.loader_data)[0]

        listing = normalize_listing(item)

        self.assertEqual(listing.source, "avito")
        self.assertEqual(listing.external_id, "8277979445")
        self.assertEqual(listing.title, "Оперативная память ddr4 16gb")
        self.assertEqual(listing.price, 5000)
        self.assertEqual(listing.price_display, "5\xa0000 ₽")
        self.assertIsNone(listing.location)
        self.assertTrue(listing.url.startswith("https://www.avito.ru/"))

    def test_normalizes_location_when_present(self) -> None:
        item = extract_items(self.loader_data)[1]

        listing = normalize_listing(item)

        self.assertEqual(listing.location, "Хабаровск")

    def test_rejects_missing_loader_data(self) -> None:
        with self.assertRaisesRegex(AvitoError, "JSON не найден"):
            extract_loader_data("<html><body>no state</body></html>")

    def test_rejects_malformed_loader_data(self) -> None:
        malformed = (
            '<script type="mime/invalid" data-mfe-state="true">'
            "{not-json}"
            "</script>"
        )

        with self.assertRaisesRegex(AvitoError, "JSON не найден"):
            extract_loader_data(malformed)

    def test_rejects_non_object_loader_payload(self) -> None:
        malformed = (
            '<script type="mime/invalid" data-mfe-state="true">'
            "[]"
            "</script>"
        )

        with self.assertRaisesRegex(AvitoError, "JSON не найден"):
            extract_loader_data(malformed)

    def test_rejects_malformed_items(self) -> None:
        with self.assertRaisesRegex(AvitoError, "неожиданный формат"):
            extract_items({"catalog": {"items": {}}})

if __name__ == "__main__":
    unittest.main()
