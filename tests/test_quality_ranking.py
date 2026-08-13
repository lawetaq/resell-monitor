from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from src.analytics import assess_condition, assess_resale, normalize_product, validate_candidate
from src.models import Listing, ListingAvailability, SearchConfig
from src.monitor import Monitor
from src.sources.base import SearchResult
from src.storage import ListingRepository


FIXTURE = Path(__file__).parent / "fixtures" / "source_quality_cases.json"


def cases() -> list[Listing]:
    return [Listing(row["source"], row["id"], row["title"], row["price"],
                    f"{row['price']} ₽", row["location"], row["url"])
            for row in json.loads(FIXTURE.read_text(encoding="utf-8"))]


class QualityAndRankingRegressionTests(unittest.TestCase):
    def test_http_200_with_only_youla_garbage_is_degraded_and_counted(self) -> None:
        class Source:
            def search(self, url: str, *, debug_dir=None) -> SearchResult:
                return SearchResult(200, cases()[:2], "fixture", "fixture")
        with tempfile.TemporaryDirectory() as root:
            with ListingRepository(Path(root) / "quality.db") as repository:
                search = SearchConfig("Youla", "youla", "https://youla.ru/habarovsk")
                scan = Monitor({"youla": Source()}, repository).scan([search])[0]
                state = repository.search_access_state(search)
                self.assertEqual(scan.health.value, "degraded")
                self.assertEqual((state.raw_items, state.valid_listings,
                                  state.rejected_items, state.priced_listings), (2, 0, 2, 0))
                self.assertEqual(repository.all(), [])

    def test_candidate_validation_accepts_real_sources_and_rejects_youla_navigation(self) -> None:
        rows = cases()
        self.assertFalse(validate_candidate(rows[0]).valid)
        self.assertFalse(validate_candidate(rows[1]).valid)
        self.assertTrue(validate_candidate(rows[2]).valid)
        self.assertTrue(validate_candidate(rows[3]).valid)

    def test_condition_ram_and_ambiguity_normalization(self) -> None:
        rows = {row.external_id: row for row in cases()}
        self.assertEqual(assess_condition(rows["faultgpu"].title).item_condition, "faulty")
        rdimm = normalize_product(rows["rdimm"].title)
        sodimm = normalize_product(rows["sodimm"].title)
        self.assertEqual((rdimm.ram_module_type, rdimm.ecc), ("RDIMM", True))
        self.assertEqual(sodimm.ram_module_type, "SODIMM")
        self.assertNotEqual(rdimm.comparable_key, sodimm.comparable_key)
        self.assertTrue(normalize_product(rows["multigpu"].title).price_ambiguous)
        self.assertTrue(normalize_product(rows["multiram"].title).price_ambiguous)
        self.assertEqual(assess_condition("RAM не стартует XMP 4400").condition_class, "OK")

    def test_fixture_pipeline_cross_source_duplicates_and_review(self) -> None:
        now = datetime.now(timezone.utc)
        valid = [row for row in cases() if validate_candidate(row).valid]
        with tempfile.TemporaryDirectory() as root:
            with ListingRepository(Path(root) / "quality.db") as repository:
                for source in ("avito", "farpost"):
                    selected = [row for row in valid if row.source == source]
                    outcomes = repository.upsert_many(selected, observed_at=now)
                    repository.record_scan_metadata(
                        SearchConfig(f"{source} components", source, f"https://{source}.test/components"),
                        selected, outcomes, [0.0] * len(selected), observed_at=now)
                rows = repository.listing_rows()
                rx = next(row for row in rows if row["external_id"] == "rx1")
                cheap_ram = next(row for row in rows if row["external_id"] == "cheapram")
                multi = next(row for row in rows if row["external_id"] == "multigpu")
                fault = next(row for row in rows if row["external_id"] == "faultgpu")
                self.assertEqual(rx["sample_size"], 3)
                self.assertEqual(rx["median_competitor_price"], 5500)
                self.assertGreater(rx["deal_score"], 0)
                self.assertNotEqual(rx["verdict"], "REJECT")
                self.assertTrue(cheap_ram["needs_review"])
                self.assertIsNotNone(cheap_ram["estimated_resale_price"])
                self.assertTrue(multi["price_ambiguous"])
                self.assertNotIn(multi["priority"], {"1", "2"})
                self.assertEqual(fault["priority"], "reject")
                key = rx["comparable_key"]
                snapshot = repository.create_market_snapshot(str(key), observed_at=now)
                assert snapshot is not None
                self.assertEqual(snapshot["sample_count"], 4)

    def test_geography_does_not_change_any_ranking_component(self) -> None:
        common = dict(asking_price=70, median=100, q1=80, sample_count=5,
                      first_seen=None)
        khabarovsk_candidate = assess_resale(**common)
        vladivostok_candidate = assess_resale(**common, geography_broadened=True)
        self.assertEqual(khabarovsk_candidate, vladivostok_candidate)
        self.assertNotIn("location", " ".join(khabarovsk_candidate.score_reasons).casefold())
        self.assertFalse(ListingAvailability.STALE is ListingAvailability.ACTIVE)

    def test_positive_gpu_and_cpu_deals_are_actionable(self) -> None:
        now = datetime.now(timezone.utc)
        listings = [
            Listing("avito", "gpu-deal", "Рабочая RTX 3060 12GB", 17_500,
                    "17 500 ₽", "Хабаровск", "https://www.avito.ru/gpu-deal",
                    availability=ListingAvailability.ACTIVE),
            *[Listing("farpost", f"gpu-{index}", "Рабочая RTX 3060 12GB", price,
                      f"{price} ₽", "Хабаровск", f"https://www.farpost.ru/gpu-{index}.html",
                      availability=ListingAvailability.ACTIVE)
              for index, price in enumerate((23_000, 23_500, 24_000, 24_500, 25_000))],
            Listing("avito", "cpu-deal", "Рабочий AMD Ryzen 5 5500", 4_750,
                    "4 750 ₽", "Хабаровск", "https://www.avito.ru/cpu-deal",
                    availability=ListingAvailability.ACTIVE),
            *[Listing("farpost", f"cpu-{index}", "Рабочий AMD Ryzen 5 5500", price,
                      f"{price} ₽", "Хабаровск", f"https://www.farpost.ru/cpu-{index}.html",
                      availability=ListingAvailability.ACTIVE)
              for index, price in enumerate((6_500, 6_800, 7_000, 7_200, 7_500))],
        ]
        with tempfile.TemporaryDirectory() as root:
            with ListingRepository(Path(root) / "positive.db") as repository:
                repository.upsert_many(listings, observed_at=now)
                for key in ("gpu:rtx-3060:12gb", "cpu:ryzen-5-5500"):
                    repository.create_market_snapshot(key, observed_at=now)
                rows = {row["external_id"]: row for row in repository.listing_rows()}
        for external_id in ("gpu-deal", "cpu-deal"):
            self.assertIn(rows[external_id]["priority"], {"1", "2"})
            self.assertFalse(rows[external_id]["needs_review"])
            self.assertIsNotNone(rows[external_id]["estimated_resale_price"])

    def test_soft_uncertainty_and_ordinary_unknown_ram_keep_estimates(self) -> None:
        ordinary_ram = assess_resale(
            asking_price=3_000, median=4_000, q1=3_600, sample_count=6,
            first_seen=None, match_confidence="medium", item_condition="unknown",
            ram_compatibility_unknown=True, geography_broadened=True,
            source_degraded=True,
        )
        self.assertFalse(ordinary_ram.needs_review)
        self.assertIsNotNone(ordinary_ram.estimated_resale_price)
        self.assertIsNotNone(ordinary_ram.expected_gross_margin)
        self.assertIsNotNone(ordinary_ram.target_buy_price)
        self.assertIsNotNone(ordinary_ram.max_buy_price)

        anomaly = assess_resale(
            asking_price=1_098, median=3_200, q1=3_000, sample_count=5,
            first_seen=None, match_confidence="medium", item_condition="unknown",
            ram_compatibility_unknown=True,
        )
        self.assertTrue(anomaly.needs_review)
        self.assertEqual(anomaly.priority, "4 / needs_review")

        fault = assess_resale(
            asking_price=5_000, median=24_000, q1=23_000, sample_count=5,
            first_seen=None, condition_class="FAULT", item_condition="faulty",
        )
        ambiguous = assess_resale(
            asking_price=4_000, median=10_000, q1=9_000, sample_count=5,
            first_seen=None, multi_item=True, price_ambiguous=True,
        )
        self.assertNotIn(fault.priority, {"1", "2"})
        self.assertNotIn(ambiguous.priority, {"1", "2"})


if __name__ == "__main__":
    unittest.main()
