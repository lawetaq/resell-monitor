from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.analytics import assess_condition, assess_resale, normalize_product
from src.config import search_presets
from src.models import Listing, ListingAvailability
from src.reporting import export_txt
from src.storage import ListingRepository


class CorrectiveNormalizationTests(unittest.TestCase):
    def test_sodimm_laptop_partial_ram_and_kits(self) -> None:
        laptop = normalize_product("Оперативная память DDR4 16 GB для ноутбука")
        partial = normalize_product("Оперативная память sodimm samsung OEM 8гб")
        self.assertEqual((laptop.product_category, laptop.ram_type,
                          laptop.ram_module_type), ("ram", "DDR4", "SODIMM"))
        self.assertEqual((partial.manufacturer, partial.ram_module_type,
                          partial.total_capacity_gb, partial.comparable_key),
                         ("Samsung", "SODIMM", 8, None))
        expected = {
            "DDR4 2x8Gb 3200Mhz": (2, 8, 16),
            "DDR4 2 x 16 GB 3200Mhz": (2, 16, 32),
            "DDR4 4x8Гб": (4, 8, 32),
            "Новая память DDR4 32GB (16+16GB) 3200Mhz": (2, 16, 32),
            "DDR4 8x2 3200 RAM": (2, 8, 16),
            "DDR4 32 GB / 2 sticks": (2, 16, 32),
        }
        for title, kit in expected.items():
            with self.subTest(title=title):
                product = normalize_product(title)
                self.assertEqual((product.module_count, product.module_capacity_gb,
                                  product.total_capacity_gb), kit)
                self.assertFalse(product.multi_item)
                self.assertFalse(product.price_ambiguous)
        assortment = normalize_product("DDR4 4/8/16/32GB")
        self.assertTrue(assortment.multi_item)
        self.assertTrue(assortment.price_ambiguous)

    def test_gpu_evidence_precedes_vram_ddr_tokens(self) -> None:
        cases = (
            ("Видеокарта R7 250 2Gb DDR5 НЕ рабочая", "R7 250"),
            ("Видеокарта Sapphire AMD Radeon HD 6670 2ГБ DDR3", "RADEON HD 6670"),
            ("RTX 3060 GDDR6", "RTX 3060"),
        )
        for title, model in cases:
            product = normalize_product(title)
            self.assertEqual(product.product_category, "gpu")
            self.assertEqual(product.gpu_model, model)
        self.assertEqual(assess_condition(cases[0][0]).condition_class, "FAULT")
        self.assertEqual(normalize_product("DDR4 16GB 3200").product_category, "ram")


class ProfitabilityGateTests(unittest.TestCase):
    def assess(self, price: int, *, q1: int = 100):
        return assess_resale(asking_price=price, median=100, q1=q1,
                             sample_count=10, first_seen=None, activity_count=10,
                             item_condition="working", liquidity_fallback=75)

    def test_actionability_is_gated_by_economics(self) -> None:
        negative = self.assess(80, q1=80)
        self.assertLessEqual(negative.expected_gross_margin or 0, 0)
        self.assertNotIn(negative.priority, {"1", "2"})
        above_max = self.assess(65, q1=80)
        self.assertGreater(65, above_max.max_buy_price or 0)
        self.assertNotIn(above_max.priority, {"1", "2"})
        self.assertEqual(above_max.priority, "3")
        self.assertEqual(self.assess(65).priority, "1")
        self.assertEqual(self.assess(75).priority, "2")

    def test_raw_band_is_preserved_but_recommendation_matches_verdict(self) -> None:
        negative = self.assess(80, q1=80)
        self.assertEqual(negative.raw_score_band, "GOOD DEAL")
        self.assertEqual(negative.recommendation, negative.verdict)
        fault = assess_resale(asking_price=20, median=100, q1=90, sample_count=10,
                              first_seen=None, condition_class="FAULT",
                              item_condition="faulty")
        self.assertEqual((fault.recommendation, fault.verdict), ("REJECT", "REJECT"))
        good = self.assess(75)
        self.assertEqual(good.recommendation, good.verdict)


class ComparableEvidenceTests(unittest.TestCase):
    def test_gpu_vram_fallback_history_conflict_and_duplicates(self) -> None:
        now = datetime.now(timezone.utc)
        active = ListingAvailability.ACTIVE
        disappeared = ListingAvailability.DISAPPEARED
        with tempfile.TemporaryDirectory() as root:
            with ListingRepository(Path(root) / "market.db") as repository:
                listings = [
                    Listing("avito", "candidate", "Рабочая GTX 1660 Super", 15_000,
                            "15 000 ₽", "Хабаровск", "https://www.avito.ru/candidate", availability=active),
                    Listing("farpost", "g1", "Рабочая GTX 1660 Super 6GB", 20_000,
                            "20 000 ₽", "Хабаровск", "https://www.farpost.ru/g1.html", availability=active),
                    Listing("farpost", "g2", "Рабочая GTX 1660 Super 6GB", 21_000,
                            "21 000 ₽", "Хабаровск", "https://www.farpost.ru/g2.html", availability=active),
                    Listing("avito", "g3", "Рабочая GTX 1660 Super", 20_500,
                            "20 500 ₽", "Хабаровск", "https://www.avito.ru/g3", availability=active),
                    Listing("avito", "r0", "Рабочая RTX 3060", 18_000,
                            "18 000 ₽", "Хабаровск", "https://www.avito.ru/r0", availability=active),
                    Listing("farpost", "r8", "Рабочая RTX 3060 8GB", 22_000,
                            "22 000 ₽", "Хабаровск", "https://www.farpost.ru/r8.html", availability=active),
                    Listing("farpost", "r12", "Рабочая RTX 3060 12GB", 25_000,
                            "25 000 ₽", "Хабаровск", "https://www.farpost.ru/r12.html", availability=active),
                ]
                repository.upsert_many(listings, observed_at=now)
                repository.create_market_snapshot("gpu:gtx-1660-super:memory-unknown", observed_at=now)
                repository.create_market_snapshot("gpu:rtx-3060:memory-unknown", observed_at=now)
                rows = {row["external_id"]: row for row in repository.listing_rows()}
                self.assertEqual(rows["candidate"]["comparable_tier"], "model_vram_relaxed")
                self.assertEqual(rows["candidate"]["sample_size"], 3)
                self.assertEqual(rows["r0"]["sample_size"], 0)

                historical = Listing("farpost", "h1", "Рабочая GTX 1660 Super 6GB", 19_500,
                                     "19 500 ₽", "Хабаровск", "https://www.farpost.ru/h1.html",
                                     availability=disappeared)
                repository.upsert(historical, observed_at=now - timedelta(days=5))
                # It remains evidence only and never becomes an actionable current listing.
                history_row = repository.listing_detail("farpost", "h1")
                self.assertFalse(history_row["is_actionable"])

    def test_recent_history_can_complete_two_active_votes_and_duplicates_count_once(self) -> None:
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as root:
            with ListingRepository(Path(root) / "history.db") as repository:
                rows = [
                    Listing("avito", "c", "Рабочая GTX 1660 Super 6GB", 15_000, "15 000 ₽",
                            "Хабаровск", "https://www.avito.ru/c", availability=ListingAvailability.ACTIVE),
                    Listing("farpost", "a1", "Рабочая GTX 1660 Super 6GB", 20_000, "20 000 ₽",
                            "Хабаровск", "https://www.farpost.ru/a1.html", availability=ListingAvailability.ACTIVE),
                    Listing("avito", "a2", "Рабочая GTX 1660 Super 6GB", 21_000, "21 000 ₽",
                            "Хабаровск", "https://www.avito.ru/a2", availability=ListingAvailability.ACTIVE),
                    Listing("farpost", "old", "Рабочая GTX 1660 Super 6GB", 19_000, "19 000 ₽",
                            "Хабаровск", "https://www.farpost.ru/old.html",
                            availability=ListingAvailability.DISAPPEARED),
                ]
                repository.upsert_many(rows[:3], observed_at=now)
                repository.upsert(rows[3], observed_at=now - timedelta(days=5))
                repository.create_market_snapshot("gpu:gtx-1660-super:6gb", observed_at=now)
                candidate = repository.listing_detail("avito", "c")
                self.assertEqual((candidate["active_sample_size"],
                                  candidate["historical_sample_size"]), (2, 1))
                self.assertTrue(candidate["historical_fallback_used"])
                self.assertIn("recent historical observations included",
                              " ".join(candidate["score_reasons"]))

        with tempfile.TemporaryDirectory() as root:
            with ListingRepository(Path(root) / "active-only.db") as repository:
                rows = [
                    Listing("avito", "c", "Рабочая GTX 1660 Super 6GB", 15_000, "15 000 ₽",
                            "Хабаровск", "https://www.avito.ru/c", availability=ListingAvailability.ACTIVE),
                    *[Listing("farpost", f"a{i}", "Рабочая GTX 1660 Super 6GB", price, f"{price} ₽",
                              "Хабаровск", f"https://www.farpost.ru/a{i}.html",
                              availability=ListingAvailability.ACTIVE)
                      for i, price in enumerate((20_000, 21_000, 22_000))],
                    Listing("farpost", "old", "Рабочая GTX 1660 Super 6GB", 1_000, "1 000 ₽",
                            "Хабаровск", "https://www.farpost.ru/old.html",
                            availability=ListingAvailability.DISAPPEARED),
                ]
                repository.upsert_many(rows[:4], observed_at=now)
                repository.upsert(rows[4], observed_at=now - timedelta(days=5))
                repository.create_market_snapshot("gpu:gtx-1660-super:6gb", observed_at=now)
                candidate = repository.listing_detail("avito", "c")
                self.assertEqual((candidate["active_sample_size"], candidate["historical_sample_size"],
                                  candidate["sample_size"]), (3, 1, 3))
                self.assertEqual(candidate["median_competitor_price"], 21_000)
                self.assertFalse(candidate["historical_fallback_used"])

        with tempfile.TemporaryDirectory() as root:
            with ListingRepository(Path(root) / "duplicates.db") as repository:
                rows = [
                    Listing("avito", "c", "Рабочая RTX 3070 8GB", 20_000, "20 000 ₽",
                            "Хабаровск", "https://www.avito.ru/c", availability=ListingAvailability.ACTIVE),
                    Listing("farpost", "one", "Рабочая RTX 3070 8GB", 30_000, "30 000 ₽",
                            "Хабаровск", "https://www.farpost.ru/one.html", availability=ListingAvailability.ACTIVE),
                    Listing("farpost", "copy1", "Магазин RTX 3070 8GB", 31_000, "31 000 ₽",
                            "Хабаровск", "https://www.farpost.ru/copy1.html", availability=ListingAvailability.ACTIVE),
                    Listing("farpost", "copy2", "Магазин RTX 3070 8GB", 31_000, "31 000 ₽",
                            "Хабаровск", "https://www.farpost.ru/copy2.html", availability=ListingAvailability.ACTIVE),
                ]
                repository.upsert_many(rows, observed_at=now)
                repository.create_market_snapshot("gpu:rtx-3070:8gb", observed_at=now)
                candidate = repository.listing_detail("avito", "c")
                self.assertEqual(candidate["sample_size"], 0)

    def test_insufficient_market_keeps_ambiguity_risk(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            with ListingRepository(Path(root) / "risk.db") as repository:
                repository.upsert(Listing("avito", "many", "GTX 1050, GTX 1650, RX 6400, RX 6500", 4000,
                                          "4000 ₽", "Хабаровск", "https://www.avito.ru/many",
                                          availability=ListingAvailability.ACTIVE))
                row = repository.listing_detail("avito", "many")
        self.assertTrue(row["requires_review"])
        self.assertTrue(row["multi_item"])
        self.assertIn("listing contains multiple products", row["risk_reasons"])
        self.assertIn("insufficient compatible market data", row["review_reasons"])


class ExportSortPresetTests(unittest.TestCase):
    def test_current_export_excludes_history_unless_requested(self) -> None:
        active = {"source": "avito", "title": "active", "price_display": "1 ₽", "url": "https://a",
                  "availability": "active", "is_actionable": True, "priority": "3"}
        old = {"source": "avito", "title": "old", "price_display": "2 ₽", "url": "https://b",
               "availability": "disappeared", "is_actionable": False, "priority": "1"}
        with tempfile.TemporaryDirectory() as root:
            current, history = Path(root) / "current.txt", Path(root) / "history.txt"
            export_txt([active, old], current)
            export_txt([active, old], history, include_history=True)
            self.assertNotIn("old", current.read_text(encoding="utf-8"))
            self.assertIn("HISTORICAL / UNAVAILABLE", history.read_text(encoding="utf-8"))
            self.assertIn("old", history.read_text(encoding="utf-8"))

    def test_repository_sorting_and_presets_are_offline(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            with ListingRepository(Path(root) / "sort.db") as repository:
                repository.upsert_many([
                    Listing("avito", "low", "item low", 100, "100 ₽", None, "https://www.avito.ru/low"),
                    Listing("avito", "high", "item high", 300, "300 ₽", None, "https://www.avito.ru/high"),
                ])
                self.assertEqual([row["external_id"] for row in repository.listing_rows(
                    sort_by="price", sort_direction="desc")], ["high", "low"])
                self.assertEqual([row["external_id"] for row in repository.listing_rows(
                    sort_by="price", sort_direction="asc")], ["low", "high"])
        presets = {row["id"]: row for row in search_presets()}
        self.assertTrue({"cpu_ryzen", "cpu_intel", "ssd", "motherboard"} <= presets.keys())
        self.assertTrue(all(not row["enabled"] for row in presets.values()))
        self.assertTrue(all(row["max_block_retries"] <= 1 for row in presets.values()))

    def test_avito_presets_are_component_scoped(self) -> None:
        presets = {row["id"]: row for row in search_presets()}
        for preset_id in ("cpu_ryzen", "cpu_intel", "ssd", "motherboard"):
            self.assertEqual(
                presets[preset_id]["path"],
                "tovary_dlya_kompyutera/komplektuyuschie",
            )
            self.assertNotIn("habarovsk", str(presets[preset_id]).casefold())

    def test_reason_output_is_stably_deduplicated(self) -> None:
        row = {"source": "avito", "title": "fault", "price_display": "1 ₽", "url": "https://a",
               "availability": "active", "priority": "reject", "verdict": "REJECT",
               "score_reasons": ["same", "distinct"], "risk_reasons": ["same"],
               "review_reasons": ["same"]}
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "reasons.txt"
            export_txt([row], path)
            self.assertEqual(path.read_text(encoding="utf-8").count("- same"), 1)

    def test_available_and_actionable_are_distinct(self) -> None:
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as root:
            with ListingRepository(Path(root) / "actionable.db") as repository:
                market = [Listing("farpost", f"m{i}", "Рабочая RTX 3070 8GB", price,
                                  f"{price} ₽", "Хабаровск", f"https://www.farpost.ru/m{i}.html",
                                  availability=ListingAvailability.ACTIVE)
                          for i, price in enumerate((30_000, 31_000, 32_000, 33_000, 34_000))]
                repository.upsert_many([
                    Listing("avito", "deal", "Рабочая RTX 3070 8GB", 20_000, "20 000 ₽",
                            "Хабаровск", "https://www.avito.ru/deal", availability=ListingAvailability.ACTIVE),
                    Listing("avito", "pass", "Рабочая RTX 3070 8GB", 40_000, "40 000 ₽",
                            "Хабаровск", "https://www.avito.ru/pass", availability=ListingAvailability.ACTIVE),
                    Listing("avito", "fault", "RTX 3070 8GB не работает", 5_000, "5 000 ₽",
                            "Хабаровск", "https://www.avito.ru/fault", availability=ListingAvailability.ACTIVE),
                    *market,
                ], observed_at=now)
                repository.upsert(Listing("avito", "stale", "Рабочая RTX 3070 8GB", 25_000, "25 000 ₽",
                                          "Хабаровск", "https://www.avito.ru/stale",
                                          availability=ListingAvailability.ACTIVE),
                                  observed_at=now - timedelta(days=4))
                repository.upsert(Listing("avito", "gone", "Рабочая RTX 3070 8GB", 25_000, "25 000 ₽",
                                          "Хабаровск", "https://www.avito.ru/gone",
                                          availability=ListingAvailability.DISAPPEARED),
                                  observed_at=now - timedelta(days=2))
                repository.create_market_snapshot("gpu:rtx-3070:8gb", observed_at=now)
                rows = {row["external_id"]: row for row in repository.listing_rows()}
        self.assertTrue(rows["deal"]["is_available"])
        self.assertTrue(rows["deal"]["is_actionable"])
        self.assertTrue(rows["pass"]["is_available"])
        self.assertFalse(rows["pass"]["is_actionable"])
        self.assertTrue(rows["fault"]["is_available"])
        self.assertFalse(rows["fault"]["is_actionable"])
        for external_id in ("stale", "gone"):
            self.assertFalse(rows[external_id]["is_available"])
            self.assertFalse(rows[external_id]["is_actionable"])


if __name__ == "__main__":
    unittest.main()
