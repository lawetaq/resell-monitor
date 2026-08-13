from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from src.analytics import FreshnessPolicy, normalize_product
from src.models import Listing, ListingAvailability, SearchConfig
from src.sources.avito import normalize_listing
from src.storage import ListingRepository


class AvailabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.database = Path(self.temporary.name) / "availability.sqlite"
        self.search = SearchConfig("GPUs", "avito", "https://example.test/search")
        self.now = datetime.now(timezone.utc)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def listing(self, identifier: str, price: int, *, availability: ListingAvailability = ListingAvailability.UNKNOWN) -> Listing:
        return Listing("avito", identifier, "RTX 3060 12GB", price, f"{price} ₽",
                       "Хабаровск", f"https://example.test/{identifier}",
                       availability=availability)

    def observe(self, repository: ListingRepository, listings: list[Listing], at: datetime) -> None:
        outcomes = repository.upsert_many(listings, observed_at=at)
        repository.record_scan_metadata(
            self.search, listings, outcomes, [0.0] * len(listings), observed_at=at
        )

    def test_recent_is_active_and_old_listing_becomes_stale(self) -> None:
        policy = FreshnessPolicy(active_for=timedelta(hours=48))
        with ListingRepository(self.database, freshness_policy=policy) as repository:
            self.observe(repository, [self.listing("recent", 20_000)], self.now)
            self.observe(repository, [self.listing("old", 1_000)], self.now - timedelta(days=365))
            rows = {row["external_id"]: row for row in repository.listing_rows()}
            self.assertEqual(rows["recent"]["availability"], "active")
            self.assertTrue(rows["recent"]["is_available"])
            self.assertFalse(rows["recent"]["is_actionable"])
            self.assertEqual(rows["old"]["availability"], "stale")
            self.assertFalse(rows["old"]["is_actionable"])
            self.assertNotIn(rows["old"]["recommendation"], {"BUY", "GOOD DEAL"})

    def test_explicit_archive_and_disappearance_remain_distinct(self) -> None:
        with ListingRepository(self.database) as repository:
            archived = self.listing("archived", 1_000, availability=ListingAvailability.ARCHIVED)
            disappeared = self.listing("gone", 2_000)
            active = [self.listing(str(index), 20_000 + index * 1_000) for index in range(3)]
            self.observe(repository, [archived, disappeared, *active], self.now)
            self.observe(repository, active, self.now + timedelta(minutes=1))
            self.observe(repository, active, self.now + timedelta(minutes=2))
            rows = {row["external_id"]: row for row in repository.listing_rows()}
            self.assertEqual(rows["archived"]["availability"], "archived")
            self.assertEqual(rows["gone"]["availability"], "disappeared")
            self.assertFalse(rows["archived"]["is_actionable"])
            self.assertFalse(rows["gone"]["is_actionable"])

    def test_unavailable_and_stale_are_excluded_but_history_remains(self) -> None:
        with ListingRepository(self.database) as repository:
            old = self.listing("old-cheap", 500)
            self.observe(repository, [old], self.now - timedelta(days=365))
            active = [self.listing(str(index), 20_000 + index * 1_000) for index in range(4)]
            self.observe(repository, active, self.now)
            key = normalize_product(active[0].title).comparable_key
            assert key is not None
            summary = repository.market_summary(key)
            assert summary is not None
            self.assertNotEqual(summary["cheapest_listing"]["external_id"], "old-cheap")
            self.assertNotEqual(summary["strongest_candidate"]["external_id"], "old-cheap")
            self.assertNotIn("old-cheap", {row["external_id"] for row in repository.top_opportunities()})
            self.assertGreaterEqual(len(repository.market_snapshots(key, days=None)), 2)

    def test_historical_only_market_has_honest_empty_current_state(self) -> None:
        with ListingRepository(self.database) as repository:
            listings = [self.listing(str(index), 10_000 + index) for index in range(3)]
            self.observe(repository, listings, self.now - timedelta(days=10))
            key = normalize_product(listings[0].title).comparable_key
            summary = repository.market_summary(str(key))
            assert summary is not None
            self.assertEqual(summary["active_listing_count"], 0)
            self.assertEqual(summary["current_market_state"], "no_active_listings")
            self.assertIsNone(summary["cheapest_listing"])
            self.assertIsNone(summary["strongest_candidate"])

    def test_legacy_migration_is_conservative_and_current_open_is_read_only(self) -> None:
        with ListingRepository(self.database) as repository:
            repository.upsert(self.listing("legacy", 100), observed_at=self.now - timedelta(days=365))
        connection = sqlite3.connect(self.database)
        connection.execute("DROP INDEX idx_listings_lifecycle")
        connection.execute("DROP INDEX idx_listings_inbox")
        connection.execute("ALTER TABLE listings DROP COLUMN availability")
        connection.execute("ALTER TABLE listings DROP COLUMN availability_updated_at")
        connection.execute("PRAGMA user_version=3")
        connection.commit()
        connection.close()
        with ListingRepository(self.database) as repository:
            self.assertEqual(repository.listing_rows()[0]["availability"], "unknown")
        connection = sqlite3.connect(self.database)
        before = connection.total_changes
        connection.close()
        with ListingRepository(self.database) as repository:
            self.assertEqual(repository.connection.total_changes, 0)
        self.assertEqual(before, 0)

    def test_avito_archive_signal_uses_existing_payload_only(self) -> None:
        listing = normalize_listing({
            "id": 1, "title": "RTX 3060", "urlPath": "/item_1",
            "status": "archived",
        })
        self.assertEqual(listing.availability, ListingAvailability.ARCHIVED)


if __name__ == "__main__":
    unittest.main()
