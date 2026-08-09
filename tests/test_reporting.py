import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from src.models import Listing
from src.reporting import export_html, export_json, export_txt
from src.storage import ListingRepository


class ReportingTests(unittest.TestCase):
    def test_all_formats_export_every_successfully_persisted_source(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with ListingRepository(root / "db.sqlite") as repository:
                repository.upsert_many(
                    [
                        Listing("avito", "1", "A & B", 10, "10 ₽", None, "https://example/a"),
                        Listing("farpost", "2", "F", 20, "20 ₽", None, "https://example/f"),
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
