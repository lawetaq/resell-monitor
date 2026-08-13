import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from src.models import Listing, ListingAvailability
from src.reporting import collision_safe_export_path, export_html, export_json, export_txt
from src.storage import ListingRepository


class ReportingTests(unittest.TestCase):
    def test_automatic_export_names_are_descriptive_and_never_overwrite(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            rows = [{"product_category": "gpu"}]
            first = collision_safe_export_path(root, "txt", rows, export_kind="top-deals")
            first.write_text("first", encoding="utf-8")
            second = collision_safe_export_path(root, "txt", rows, export_kind="top-deals")
            self.assertNotEqual(first, second)
            self.assertRegex(first.name, r"^top-deals_gpu_\d{4}-\d{2}-\d{2}_\d{6}\.txt$")
            self.assertFalse(second.exists())

    def test_all_formats_export_every_successfully_persisted_source(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with ListingRepository(root / "db.sqlite") as repository:
                repository.upsert_many(
                    [
                        Listing("avito", "1", "A & B", 10, "10 ₽", None, "https://www.avito.ru/item_1",
                                availability=ListingAvailability.ACTIVE),
                        Listing("farpost", "2", "FarPost item", 20, "20 ₽", None, "https://www.farpost.ru/item-2.html",
                                availability=ListingAvailability.ACTIVE),
                    ]
                )
                rows = repository.all()
            export_json(rows, root / "out.json")
            export_txt(rows, root / "out.txt")
            export_html(rows, root / "out.html")
            self.assertEqual({item["source"] for item in json.loads((root / "out.json").read_text())}, {"avito", "farpost"})
            self.assertIn("[avito]", (root / "out.txt").read_text())
            html = (root / "out.html").read_text()
            self.assertIn("avito", html)
            self.assertIn("farpost", html)
            self.assertIn("A &amp; B", html)
