from __future__ import annotations

import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from src.config import save_searches
from src.gui.service import GuiService
from src.models import Listing, SearchConfig
from src.storage import ListingRepository


class MarketUXTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.database = self.root / "market.sqlite"
        self.config = self.root / "searches.json"
        self.scan_calls: list[object] = []
        save_searches(
            self.config,
            [SearchConfig("Components", "avito", "https://www.avito.ru/components")],
        )
        listings = [
            Listing("avito", "g1", "RTX 3060 12GB", 20_000, "20 000 ₽", "Хабаровск", "https://www.avito.ru/g1"),
            Listing("avito", "g2", "RTX 3060 12GB", 25_000, "25 000 ₽", "Хабаровск", "https://www.avito.ru/g2"),
            Listing("avito", "g3", "RTX 3060 12GB", 30_000, "30 000 ₽", "Хабаровск", "https://www.avito.ru/g3"),
            Listing("avito", "g4", "RTX 3060 без указания памяти", 24_000, "24 000 ₽", "Хабаровск", "https://www.avito.ru/g4"),
            Listing("avito", "c1", "AMD Ryzen 5 5600", 10_000, "10 000 ₽", "Хабаровск", "https://www.avito.ru/c1"),
            Listing("avito", "r1", "DDR4 16GB 3200 MHz", 4_000, "4 000 ₽", "Хабаровск", "https://www.avito.ru/r1"),
            Listing("avito", "r2", "DDR5 32GB 5200 MHz", 8_000, "8 000 ₽", "Хабаровск", "https://www.avito.ru/r2"),
            Listing("avito", "s1", "SSD NVMe 1TB", 7_000, "7 000 ₽", "Хабаровск", "https://www.avito.ru/s1"),
        ]
        search = SearchConfig("Components", "avito", "https://www.avito.ru/components")
        now = datetime.now(timezone.utc)
        with ListingRepository(self.database) as repository:
            outcomes = repository.upsert_many(listings, observed_at=now)
            repository.record_scan_metadata(
                search, listings, outcomes, [0.0] * len(listings), observed_at=now
            )
        self.service = GuiService(
            config_path=self.config,
            database_path=self.database,
            output_dir=self.root / "output",
            scan_runner=lambda searches, progress: self.scan_calls.append(searches) or [],
        )

    def tearDown(self) -> None:
        self.service.close()
        self.temporary.cleanup()

    def test_category_filtering_and_all_category_search(self) -> None:
        all_products = self.service.market_search("")
        self.assertEqual({row["product_category"] for row in all_products},
                         {"gpu", "cpu", "ram", "ssd"})
        gpu_products = self.service.market_search("", "gpu")
        self.assertTrue(gpu_products)
        self.assertEqual({row["product_category"] for row in gpu_products}, {"gpu"})

    def test_cpu_ram_and_ssd_normalized_search(self) -> None:
        self.assertEqual(self.service.market_search("Ryzen 5 5600")[0]["product_category"], "cpu")
        self.assertEqual(self.service.market_search("DDR4 16GB")[0]["product_category"], "ram")
        self.assertEqual(self.service.market_search("DDR5 32GB 5200")[0]["product_category"], "ram")
        self.assertEqual(self.service.market_search("NVMe 1TB")[0]["product_category"], "ssd")
        self.assertEqual(self.service.market_search("DDR4", "gpu"), [])

    def test_clear_gpu_label_for_unknown_vram(self) -> None:
        product = self.service.market_search("RTX 3060", "gpu")
        unknown = next(row for row in product if row["variant"] == "memory-unknown")
        self.assertEqual(unknown["display_label"], "RTX 3060 · VRAM unknown")
        self.assertIn("memory-unknown", unknown["comparable_key"])

    def test_candidate_references_open_through_shared_listing_detail(self) -> None:
        key = self.service.market_search("RTX 3060 12GB", "gpu")[0]["comparable_key"]
        market = self.service.market_product(str(key))
        summary = market["summary"]
        for field in ("cheapest_listing", "strongest_candidate"):
            candidate = summary[field]
            self.assertIsNotNone(candidate)
            detail = self.service.listing_detail(
                str(candidate["source"]), str(candidate["external_id"])
            )
            self.assertEqual(detail["title"], candidate["title"])
            self.assertIn("price_history", detail)
        opportunity = self.service.top_opportunities()[0]
        detail = self.service.listing_detail(
            str(opportunity["source"]), str(opportunity["external_id"])
        )
        self.assertEqual(detail["external_id"], opportunity["external_id"])

    def test_market_filtering_never_invokes_scan_runner(self) -> None:
        self.service.market_search("RTX 3060", "gpu")
        self.service.market_search("Ryzen 5 5600", "cpu")
        self.service.market_search("DDR5 32GB", "ram")
        self.service.market_search("NVMe 1TB", "ssd")
        self.assertEqual(self.scan_calls, [])

    def test_static_market_layout_and_shared_click_binding(self) -> None:
        static = Path("src/gui/static")
        html = (static / "index.html").read_text(encoding="utf-8")
        css = (static / "styles.css").read_text(encoding="utf-8")
        script = (static / "app.js").read_text(encoding="utf-8")
        self.assertIn('id="market-category"', html)
        self.assertLess(html.index('id="top-opportunities"'), html.index('id="used-chart"'))
        self.assertIn("max-height:calc(100vh - 330px)", css)
        self.assertIn("overflow-y:auto", css)
        self.assertIn("bindListingCandidates(candidates)", script)
        self.assertIn("bindListingCandidates(opportunityRoot)", script)
        self.assertIn("openListing(x.dataset.source,x.dataset.id)", script)


if __name__ == "__main__":
    unittest.main()
