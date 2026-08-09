from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from src.analytics import aggregate_market, assess_condition, assess_resale, normalize_product
from src.gui.presentation import format_market_summary, score_badge
from src.gui.service import GuiService
from src.models import Listing, SearchConfig
from src.retail import RetailPriceObservation
from src.storage import ListingRepository


class ProductNormalizationTests(unittest.TestCase):
    def test_gpu_memory_and_suffix_variants_remain_distinct(self) -> None:
        keys = {
            normalize_product("GeForce RTX 3060 8 GB").comparable_key,
            normalize_product("GeForce RTX 3060 12GB").comparable_key,
            normalize_product("GeForce RTX 3060 Ti 8GB").comparable_key,
        }
        self.assertEqual(len(keys), 3)

    def test_rx_suffix_remains_distinct(self) -> None:
        self.assertNotEqual(normalize_product("RX 6600 8GB").comparable_key,
                            normalize_product("RX 6600 XT 8GB").comparable_key)
        self.assertNotEqual(normalize_product("RX 6700 12GB").comparable_key,
                            normalize_product("RX 6700 XT 12GB").comparable_key)

    def test_ram_generation_capacity_and_speed(self) -> None:
        d4 = normalize_product("Kingston DDR4 16 GB 3200 MHz")
        d5 = normalize_product("Kingston DDR5 16GB 5600 MHz")
        d4_32 = normalize_product("Kingston DDR4 32GB 3200")
        self.assertEqual(d4.product_category, "ram")
        self.assertEqual(len({d4.comparable_key, d5.comparable_key, d4_32.comparable_key}), 3)

    def test_cpu_and_ssd_normalization_and_conservative_unknown(self) -> None:
        self.assertIn("i5-12400", normalize_product("Intel Core i5-12400F").comparable_key or "")
        self.assertNotEqual(normalize_product("SSD NVMe 512GB").comparable_key,
                            normalize_product("SSD SATA 512GB").comparable_key)
        self.assertIsNone(normalize_product("Видеокарта игровая").comparable_key)


class ConditionTests(unittest.TestCase):
    def test_fault_and_risk_phrases(self) -> None:
        self.assertEqual(assess_condition("RTX 3060 не работает").condition_class, "FAULT")
        risk = assess_condition("Видеокарта после майнинга")
        self.assertEqual(risk.condition_class, "RISK")
        self.assertIn("после майнинга", risk.matched_warning_phrases)

    def test_negation_and_no_artifacts_are_safe(self) -> None:
        self.assertEqual(assess_condition("Не ремонтировалась, работает").condition_class, "OK")
        self.assertEqual(assess_condition("Работает, артефактов нет").condition_class, "OK")


class MarketMathTests(unittest.TestCase):
    def test_median_quartiles_and_outlier_resistant_bounds(self) -> None:
        snapshot = aggregate_market("gpu:x", [100, 110, 120, 130, 10_000],
                                    datetime.now(timezone.utc))
        assert snapshot is not None
        self.assertEqual(snapshot.median, 120)
        self.assertEqual(snapshot.q1, 110)
        self.assertEqual(snapshot.q3, 130)
        self.assertEqual(snapshot.sample_count, 5)
        self.assertEqual(snapshot.maximum, 130)

    def test_insufficient_samples_do_not_create_false_score(self) -> None:
        result = assess_resale(asking_price=50, median=100, q1=80, sample_count=2,
                               first_seen=None)
        self.assertIsNone(result.score)
        self.assertEqual(result.recommendation, "INSUFFICIENT DATA")

    def test_estimated_resale_margin_and_fault_independence(self) -> None:
        result = assess_resale(asking_price=50, median=100, q1=80, sample_count=10,
                               first_seen=datetime.now(timezone.utc), activity_count=10)
        self.assertEqual(result.estimated_resale_price, 78)
        self.assertEqual(result.expected_gross_margin, 28)
        self.assertGreaterEqual(result.score or 0, 80)
        self.assertEqual(assess_condition("RTX 3060 на запчасти").condition_class, "FAULT")

    def test_falling_market_reduces_score_and_resale(self) -> None:
        stable = assess_resale(asking_price=60, median=100, q1=80, sample_count=10,
                               first_seen=None, trend_30d=0)
        falling = assess_resale(asking_price=60, median=100, q1=80, sample_count=10,
                                first_seen=None, trend_30d=-10)
        self.assertLess(falling.score or 0, stable.score or 0)
        self.assertLess(falling.estimated_resale_price or 0, stable.estimated_resale_price or 0)


class MarketPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.db = Path(self.tmp.name) / "analytics.sqlite"
        self.config = Path(self.tmp.name) / "searches.json"
        self.config.write_text("[]", encoding="utf-8")
        self.search = SearchConfig("GPUs", "avito", "https://www.avito.ru/gpu")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _listings(self, prices: list[int], suffix: str = "") -> list[Listing]:
        return [Listing("avito", str(i), f"RTX 3060 12GB {suffix}", price,
                        f"{price} ₽", "Хабаровск", f"https://avito.ru/{i}")
                for i, price in enumerate(prices)]

    def _observe(self, repository: ListingRepository, prices: list[int], at: datetime) -> str:
        listings = self._listings(prices)
        outcomes = repository.upsert_many(listings, observed_at=at)
        repository.record_scan_metadata(self.search, listings, outcomes,
                                        [0.0] * len(listings), observed_at=at)
        return normalize_product(listings[0].title).comparable_key or ""

    def test_snapshots_trends_dynamic_score_and_top_opportunities(self) -> None:
        now = datetime.now(timezone.utc)
        with ListingRepository(self.db) as repository:
            key = self._observe(repository, [90, 100, 110, 120, 130], now - timedelta(days=20))
            self._observe(repository, [60, 80, 90, 100, 110], now)
            snapshots = repository.market_snapshots(key, days=None)
            self.assertEqual(len(snapshots), 2)
            summary = repository.market_summary(key)
            assert summary is not None
            self.assertLess(summary["trend_30d_percent"], 0)
            rows = repository.listing_rows()
            self.assertEqual(rows[0]["market_trend"], "FALLING")
            self.assertTrue(repository.top_opportunities())

    def test_disappeared_is_turnover_signal_not_sale(self) -> None:
        now = datetime.now(timezone.utc)
        with ListingRepository(self.db) as repository:
            key = self._observe(repository, [90, 100, 110], now - timedelta(days=1))
            listings = self._listings([90, 100])
            outcomes = repository.upsert_many(listings, observed_at=now)
            repository.record_scan_metadata(self.search, listings, outcomes, [0, 0], observed_at=now)
            summary = repository.market_summary(key)
            assert summary is not None
            self.assertEqual(summary["disappeared_count"], 1)
            self.assertIn("turnover signal", format_market_summary(summary))

    def test_retail_abstraction_storage_without_affecting_ranking(self) -> None:
        now = datetime.now(timezone.utc)
        with ListingRepository(self.db) as repository:
            key = self._observe(repository, [60, 80, 90, 100, 110], now)
            before = repository.listing_rows()[0]["score"]
            repository.add_retail_observation(RetailPriceObservation(
                key, "Example retailer", 140, now, "https://example.test/item"))
            self.assertEqual(repository.retail_observations(key)[0]["price"], 140)
            self.assertEqual(repository.listing_rows()[0]["score"], before)

    def test_gui_local_search_ranges_summary_and_badges(self) -> None:
        now = datetime.now(timezone.utc)
        with ListingRepository(self.db) as repository:
            key = self._observe(repository, [60, 80, 90, 100, 110], now)
        service = GuiService(config_path=self.config, database_path=self.db,
                             output_dir=Path(self.tmp.name) / "out")
        try:
            self.assertEqual(service.market_search("RTX 3060")[0]["comparable_key"], key)
            for range_name in ("7D", "30D", "90D", "ALL"):
                self.assertEqual(service.market_product(key, range_name)["range"], range_name)
            self.assertTrue(service.top_opportunities())
        finally:
            service.close()
        self.assertEqual(score_badge(86), {"label": "BUY", "tone": "strong-green"})
        self.assertEqual(score_badge(72)["label"], "GOOD DEAL")
        self.assertEqual(score_badge(50)["label"], "NEGOTIATE")
        self.assertEqual(score_badge(20)["label"], "PASS")
        self.assertEqual(score_badge(None)["label"], "INSUFFICIENT DATA")


if __name__ == "__main__":
    unittest.main()
