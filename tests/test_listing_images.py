from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from src.models import Listing, normalize_external_image_url
from src.sources.avito import extract_items, extract_loader_data, normalize_listing
from src.sources.avito_playwright_parser import extract_dom_listings
from src.sources.farpost import parse_farpost_html
from src.sources.youla import parse_youla_html
from src.storage import ListingRepository


ROOT = Path(__file__).resolve().parents[1]


class ListingImageModelTests(unittest.TestCase):
    def listing(self, image: str | None = None) -> Listing:
        return Listing(
            "avito", "image-1", "RTX 3060", 20_000, "20 000 ₽", None,
            "https://www.avito.ru/item_1", primary_image_url=image,
        )

    def test_listing_accepts_missing_and_valid_image(self) -> None:
        self.assertIsNone(self.listing().primary_image_url)
        url = "https://70.img.avito.st/image/preview"
        self.assertEqual(self.listing(url).primary_image_url, url)

    def test_invalid_or_local_image_urls_are_ignored(self) -> None:
        for value in (
            "javascript:alert(1)", "file:///tmp/a.jpg", "data:image/png;base64,a",
            "http://localhost/a.jpg", "http://127.0.0.1/a.jpg",
            "http://10.0.0.1/a.jpg", "https://user:secret@example.com/a.jpg",
            "not a URL",
        ):
            with self.subTest(value=value):
                self.assertIsNone(normalize_external_image_url(value))
                self.assertIsNone(self.listing(value).primary_image_url)


class ListingImageParserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.avito_html = (ROOT / "output" / "avito_response.html").read_text(
            encoding="utf-8"
        )

    def test_avito_embedded_and_network_item_extracts_preview(self) -> None:
        item = extract_items(extract_loader_data(self.avito_html))[0]
        listing = normalize_listing(item)
        self.assertEqual(
            listing.primary_image_url,
            item["images"][0]["236x236"],
        )

    def test_avito_dom_extracts_search_card_image(self) -> None:
        listing = extract_dom_listings(self.avito_html)[0]
        self.assertIsNotNone(listing.primary_image_url)
        self.assertTrue(listing.primary_image_url.startswith("https://"))

    def test_missing_or_invalid_avito_image_does_not_fail_listing(self) -> None:
        base = {"id": 1, "title": "GPU", "urlPath": "/item_1"}
        self.assertIsNone(normalize_listing(base).primary_image_url)
        invalid = {**base, "images": [{"236x236": "javascript:alert(1)"}]}
        self.assertIsNone(normalize_listing(invalid).primary_image_url)
        foreign = {**base, "images": [{"236x236": "https://example.com/a.jpg"}]}
        self.assertIsNone(normalize_listing(foreign).primary_image_url)

    def test_current_farpost_and_youla_fixtures_have_no_image_data(self) -> None:
        farpost = parse_farpost_html(
            (ROOT / "tests" / "fixtures" / "farpost.html").read_text()
        )[0]
        youla = parse_youla_html(
            (ROOT / "tests" / "fixtures" / "youla.html").read_text()
        )[0]
        self.assertIsNone(farpost.primary_image_url)
        self.assertIsNone(youla.primary_image_url)


class ListingImageStorageTests(unittest.TestCase):
    def test_roundtrip_preserves_image_and_missing_observation_does_not_erase_it(self) -> None:
        with TemporaryDirectory() as directory:
            database = Path(directory) / "monitor.sqlite"
            first_at = datetime(2026, 8, 13, tzinfo=timezone.utc)
            url = "https://70.img.avito.st/image/preview"
            with ListingRepository(database) as repository:
                first = Listing(
                    "avito", "1", "RTX 3060", 20_000, "20 000 ₽", None,
                    "https://www.avito.ru/item_1", primary_image_url=url,
                )
                repository.upsert(first, observed_at=first_at)
                without_image = Listing(
                    "avito", "1", "RTX 3060", 20_000, "20 000 ₽", None,
                    "https://www.avito.ru/item_1",
                )
                outcome = repository.upsert(
                    without_image, observed_at=first_at + timedelta(minutes=5)
                )
                row = repository.listing_detail("avito", "1")
                self.assertEqual(row["primary_image_url"], url)
                self.assertFalse(outcome.price_changed)
                self.assertEqual(len(row["price_history"]), 1)
                replacement_url = "https://80.img.avito.st/image/new-preview"
                with_new_image = Listing(
                    "avito", "1", "RTX 3060", 20_000, "20 000 ₽", None,
                    "https://www.avito.ru/item_1",
                    primary_image_url=replacement_url,
                )
                image_outcome = repository.upsert(
                    with_new_image, observed_at=first_at + timedelta(minutes=10)
                )
                updated = repository.listing_detail("avito", "1")
                self.assertEqual(updated["primary_image_url"], replacement_url)
                self.assertFalse(image_outcome.price_changed)
                self.assertEqual(len(updated["price_history"]), 1)

    def test_v9_migration_adds_nullable_image_column_and_current_open_is_write_free(self) -> None:
        with TemporaryDirectory() as directory:
            database = Path(directory) / "monitor.sqlite"
            with ListingRepository(database) as repository:
                repository.upsert(Listing(
                    "avito", "old", "GPU", 1, "1 ₽", None,
                    "https://www.avito.ru/item_old",
                ))
            with sqlite3.connect(database) as connection:
                connection.execute("PRAGMA user_version=9")
                connection.execute("ALTER TABLE listings DROP COLUMN primary_image_url")
                connection.commit()
            with ListingRepository(database) as repository:
                row = repository.listing_detail("avito", "old")
                self.assertIsNone(row["primary_image_url"])
                self.assertEqual(repository.connection.execute("PRAGMA user_version").fetchone()[0], 10)


class ListingImageGuiContractTests(unittest.TestCase):
    def test_thumbnail_detail_placeholder_and_localization_contract(self) -> None:
        app = (ROOT / "src" / "gui" / "static" / "app.js").read_text()
        css = (ROOT / "src" / "gui" / "static" / "styles.css").read_text()
        i18n = (ROOT / "src" / "gui" / "static" / "i18n.js").read_text()
        self.assertIn("l.primary_image_url", app)
        self.assertIn("loading=\"lazy\"", app)
        self.assertIn("decoding=\"async\"", app)
        self.assertIn("referrerpolicy=\"no-referrer\"", app)
        self.assertIn("listing-image-placeholder", app)
        self.assertIn("listingImage(l.primary_image_url,'detail')", app)
        self.assertIn("object-fit:cover", css)
        self.assertIn("object-fit:contain", css)
        self.assertIn("'listings.image':'Listing image'", i18n)
        self.assertIn("'listings.image':'Изображение объявления'", i18n)


if __name__ == "__main__":
    unittest.main()
