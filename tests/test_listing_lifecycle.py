from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.analytics import FreshnessPolicy, normalize_product
from src.gui.service import GuiService
from src.models import Listing, ListingAvailability, ListingStatus, SearchConfig
from src.monitor import Monitor
from src.sources.base import HealthState, SearchResult
from src.storage import ListingRepository


class ListingLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "lifecycle.sqlite"
        self.search = SearchConfig("GPUs", "avito", "https://example.test/gpu")
        self.now = datetime.now(timezone.utc)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def listing(self, identifier: str, price: int = 20_000) -> Listing:
        return Listing("avito", identifier, "RTX 3060 12GB", price, f"{price} ₽",
                       "Хабаровск", f"https://example.test/{identifier}")

    def scan(self, repository: ListingRepository, listings: list[Listing], at: datetime,
             *, trustworthy: bool = True) -> None:
        outcomes = repository.upsert_many(listings, observed_at=at)
        repository.record_scan_metadata(
            self.search, listings, outcomes, [0.0] * len(listings),
            observed_at=at, trustworthy=trustworthy,
        )

    def test_failed_or_blocked_equivalent_result_does_not_count_absence(self) -> None:
        with ListingRepository(self.database) as repository:
            self.scan(repository, [self.listing("one")], self.now)
            self.scan(repository, [], self.now + timedelta(minutes=1), trustworthy=False)
            self.scan(repository, [], self.now + timedelta(minutes=2), trustworthy=False)
            row = repository.connection.execute(
                "SELECT availability FROM listings WHERE external_id='one'"
            ).fetchone()
            count = repository.connection.execute(
                "SELECT missing_success_count FROM listing_searches WHERE external_id='one'"
            ).fetchone()
            self.assertEqual(row[0], "active")
            self.assertEqual(count[0], 0)

    def test_degraded_incomplete_monitor_scan_does_not_count_absence(self) -> None:
        class DegradedSource:
            def search(self, url: str, *, debug_dir=None) -> SearchResult:
                return SearchResult(200, [], "fixture", "fixture",
                                    health=HealthState.DEGRADED,
                                    error="incomplete response")

        with ListingRepository(self.database) as repository:
            self.scan(repository, [self.listing("one")], self.now)
            monitor = Monitor({"avito": DegradedSource()}, repository)
            monitor.scan([self.search])
            monitor.scan([self.search])
            row = repository.connection.execute(
                """SELECT l.availability,ls.missing_success_count FROM listings l
                   JOIN listing_searches ls USING(source,external_id)"""
            ).fetchone()
            self.assertEqual(tuple(row), ("active", 0))
            state = repository.search_access_state(self.search)
            self.assertEqual(state.health, "degraded")
            self.assertEqual(state.last_error, "incomplete response")
            self.assertEqual(repository.connection.execute(
                "SELECT COUNT(*) FROM search_scan_runs").fetchone()[0], 1)
            self.assertEqual(len(repository.source_attempts("avito", "GPUs")), 2)

    def test_obsolete_or_disabled_search_association_cannot_block_consensus(self) -> None:
        secondary = SearchConfig("Old GPUs", "avito", "https://example.test/old")
        item = self.listing("shared")
        with ListingRepository(self.database) as repository:
            outcomes = repository.upsert_many([item], observed_at=self.now)
            repository.record_scan_metadata(
                self.search, [item], outcomes, [0.0], observed_at=self.now,
                authoritative_search_names={self.search.name, secondary.name},
            )
            outcomes = repository.upsert_many([item], observed_at=self.now + timedelta(seconds=1))
            repository.record_scan_metadata(
                secondary, [item], outcomes, [0.0],
                observed_at=self.now + timedelta(seconds=1),
                authoritative_search_names={self.search.name, secondary.name},
            )
            self.assertEqual(repository.connection.execute(
                "SELECT COUNT(*) FROM listing_searches WHERE external_id='shared'"
            ).fetchone()[0], 2)
            class EmptySource:
                def search(self, url: str, *, debug_dir=None) -> SearchResult:
                    return SearchResult(200, [], "fixture", "fixture")

            disabled = SearchConfig(secondary.name, secondary.source, secondary.url,
                                    enabled=False)
            times = iter([
                self.now + timedelta(minutes=1), self.now + timedelta(minutes=1),
                self.now + timedelta(minutes=1), self.now + timedelta(minutes=2),
                self.now + timedelta(minutes=2), self.now + timedelta(minutes=2),
            ])
            monitor = Monitor(
                {"avito": EmptySource()}, repository, clock=lambda: next(times),
                authoritative_searches=[self.search, disabled],
            )
            monitor.scan([self.search])
            monitor.scan([self.search])
            row = repository.connection.execute(
                "SELECT availability FROM listings WHERE external_id='shared'"
            ).fetchone()
            self.assertEqual(row[0], "disappeared")

    def test_two_trustworthy_absences_disappear_and_reappearance_restores(self) -> None:
        with ListingRepository(self.database) as repository:
            item = self.listing("one")
            self.scan(repository, [item], self.now)
            self.scan(repository, [], self.now + timedelta(minutes=1))
            self.assertEqual(repository.connection.execute(
                "SELECT availability FROM listings").fetchone()[0], "active")
            self.scan(repository, [], self.now + timedelta(minutes=2))
            self.assertEqual(repository.connection.execute(
                "SELECT availability FROM listings").fetchone()[0], "disappeared")
            self.scan(repository, [item], self.now + timedelta(minutes=3))
            row = repository.connection.execute(
                """SELECT l.availability,ls.missing_success_count FROM listings l
                   JOIN listing_searches ls USING(source,external_id)"""
            ).fetchone()
            self.assertEqual(tuple(row), ("active", 0))

    def test_inbox_age_review_and_priority_are_separate(self) -> None:
        policy = FreshnessPolicy(active_for=timedelta(days=30))
        with ListingRepository(self.database, freshness_policy=policy) as repository:
            ordinary = {"availability": "active", "first_seen": (self.now - timedelta(days=6)).isoformat(),
                        "last_seen": self.now.isoformat(), "status": "new", "priority": "3",
                        "verdict": "NEGOTIATE", "condition_class": "OK"}
            repository._apply_availability(ordinary)
            self.assertFalse(ordinary["in_working_inbox"])
            protected = dict(ordinary, priority="1")
            repository._apply_availability(protected)
            self.assertTrue(protected["in_working_inbox"])
            reviewed = dict(protected, status="reviewed")
            repository._apply_availability(reviewed)
            self.assertEqual(reviewed["availability"], "active")
            self.assertFalse(reviewed["in_working_inbox"])

    def test_cleanup_preview_is_read_only_and_apply_preserves_history(self) -> None:
        policy = FreshnessPolicy(active_for=timedelta(days=30))
        with ListingRepository(self.database, freshness_policy=policy) as repository:
            item = self.listing("old")
            old = self.now - timedelta(days=6)
            self.scan(repository, [item], old)
            repository.upsert_many([item], observed_at=self.now)
            before_history = len(repository.history("avito", "old"))
            before = repository.connection.total_changes
            preview = repository.cleanup_preview(now=self.now)
            self.assertEqual(preview["aged_out_count"], 1)
            self.assertEqual(repository.connection.total_changes, before)
            applied = repository.apply_cleanup(now=self.now)
            self.assertEqual(applied["aged_out_count"], preview["aged_out_count"])
            self.assertEqual(len(repository.history("avito", "old")), before_history)
            self.assertIsNotNone(repository.connection.execute(
                "SELECT inbox_aged_out_at FROM listings WHERE external_id='old'"
            ).fetchone()[0])

    def test_disappeared_archives_without_deletion_or_market_loss(self) -> None:
        with ListingRepository(self.database) as repository:
            items = [self.listing(str(index), 20_000 + index * 1_000) for index in range(3)]
            self.scan(repository, items, self.now - timedelta(days=20))
            self.scan(repository, [], self.now - timedelta(days=19))
            self.scan(repository, [], self.now - timedelta(days=18))
            key = normalize_product(items[0].title).comparable_key
            snapshots_before = len(repository.market_snapshots(str(key), days=None))
            preview = repository.cleanup_preview(now=self.now, archive_days=14)
            self.assertEqual(preview["archive_count"], 3)
            repository.apply_cleanup(now=self.now, archive_days=14)
            self.assertEqual(repository.connection.execute(
                "SELECT COUNT(*) FROM listings").fetchone()[0], 3)
            self.assertEqual(repository.connection.execute(
                "SELECT COUNT(*) FROM listings WHERE availability='archived'").fetchone()[0], 3)
            self.assertGreaterEqual(len(repository.market_snapshots(str(key), days=None)), snapshots_before)

    def test_default_settings_persist_and_cleanup_never_scans(self) -> None:
        calls: list[object] = []
        base = Path(self.temp.name)
        service = GuiService(config_path=base / "searches.json", database_path=self.database,
                             output_dir=base / "out",
                             scan_runner=lambda searches, progress: calls.append(searches) or [])
        settings = service.settings()
        self.assertEqual((settings["unreviewed_inbox_days"], settings["disappearance_success_scans"],
                          settings["archive_disappeared_days"], settings["archive_retention_days"]),
                         (5, 2, 14, 180))
        service.update_settings({"unreviewed_inbox_days": 7, "archive_retention_days": 90})
        service.cleanup_preview()
        service.apply_cleanup(remove_from_inbox=True, archive_disappeared=True)
        self.assertEqual(calls, [])
        service.close()

    def test_filters_keep_review_and_lifecycle_independent(self) -> None:
        with ListingRepository(self.database) as repository:
            reviewed = self.listing("reviewed")
            archived = self.listing("archived")
            self.scan(repository, [reviewed, archived], self.now)
            repository.set_status("avito", "reviewed", ListingStatus.REVIEWED)
            repository.archive_listings([("avito", "archived")], observed_at=self.now)
            self.assertEqual(
                [row["external_id"] for row in repository.listing_rows(status="reviewed")],
                ["reviewed"],
            )
            self.assertEqual(
                [row["external_id"] for row in repository.listing_rows(availability="archived")],
                ["archived"],
            )
            self.assertEqual(repository.listing_rows(status="reviewed")[0]["availability"],
                             "active")


if __name__ == "__main__":
    unittest.main()
