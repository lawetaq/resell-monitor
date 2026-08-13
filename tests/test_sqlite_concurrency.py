from __future__ import annotations

import json
import sqlite3
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from src.config import save_searches
from src.gui.service import GuiService
from src.models import Listing, SearchConfig
from src.storage import ListingRepository


WRITE_PREFIXES = ("BEGIN", "CREATE", "ALTER", "INSERT", "UPDATE", "DELETE", "REPLACE", "DROP")
ORIGINAL_CONNECT = sqlite3.connect


class TrackingConnection(sqlite3.Connection):
    instances: list[TrackingConnection] = []

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.closed_by_repository = False
        self.statements: list[str] = []
        self.set_trace_callback(self.statements.append)
        self.instances.append(self)

    def close(self) -> None:
        self.closed_by_repository = True
        super().close()


def write_statements(connection: TrackingConnection) -> list[str]:
    return [statement for statement in connection.statements
            if statement.lstrip().upper().startswith(WRITE_PREFIXES)]


def tracking_connect(*args, **kwargs) -> TrackingConnection:
    return ORIGINAL_CONNECT(*args, factory=TrackingConnection, **kwargs)


class SQLiteMigrationRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.database = self.root / "monitor.sqlite"
        self.config = self.root / "searches.json"
        self.output = self.root / "output"
        self.search = SearchConfig("GPUs", "avito", "https://www.avito.ru/gpus")
        save_searches(self.config, [self.search])

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _seed(self, count: int = 4) -> None:
        now = datetime.now(timezone.utc)
        listings = [
            Listing("avito", str(index),
                    "Нераспознанный компьютерный компонент" if index == 0 else "RTX 3060 12GB",
                    20_000 + index * 1_000, f"{20 + index} 000 ₽", "Хабаровск",
                    f"https://www.avito.ru/{index}")
            for index in range(count)
        ]
        with ListingRepository(self.database) as repository:
            outcomes = repository.upsert_many(listings, observed_at=now)
            repository.record_scan_metadata(
                self.search, listings, outcomes, [0.0] * count, observed_at=now
            )

    def test_opening_current_repository_performs_no_writes(self) -> None:
        self._seed()
        TrackingConnection.instances.clear()
        with patch("src.storage.sqlite.sqlite3.connect", side_effect=tracking_connect):
            with ListingRepository(self.database):
                pass
        self.assertEqual(len(TrackingConnection.instances), 1)
        self.assertEqual(write_statements(TrackingConnection.instances[0]), [])

    def test_unmatched_listing_is_backfilled_once(self) -> None:
        self._seed(1)
        with sqlite3.connect(self.database) as connection:
            row = connection.execute(
                "SELECT comparable_key,match_confidence,analytics_backfill_version FROM listings"
            ).fetchone()
        self.assertEqual(row, (None, "insufficient", ListingRepository.ANALYTICS_BACKFILL_VERSION))
        TrackingConnection.instances.clear()
        with patch("src.storage.sqlite.sqlite3.connect", side_effect=tracking_connect):
            with ListingRepository(self.database):
                pass
        self.assertFalse(write_statements(TrackingConnection.instances[0]))

    def test_failed_migration_rolls_back_and_closes(self) -> None:
        TrackingConnection.instances.clear()

        def fail(_repository) -> None:
            _repository.connection.execute(
                "INSERT INTO app_settings(key,value) VALUES ('partial','yes')"
            )
            raise RuntimeError("migration failed")

        with patch("src.storage.sqlite.sqlite3.connect", side_effect=tracking_connect), \
             patch.object(ListingRepository, "_migrate_analytics_v2", fail):
            with self.assertRaisesRegex(RuntimeError, "migration failed"):
                ListingRepository(self.database)
        self.assertTrue(TrackingConnection.instances[0].closed_by_repository)
        with sqlite3.connect(self.database) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 0)
            tables = connection.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
            ).fetchone()[0]
        self.assertEqual(tables, 0)

    def test_existing_rows_and_searches_survive_version_zero_upgrade(self) -> None:
        connection = sqlite3.connect(self.database)
        connection.executescript("""
            CREATE TABLE listings (
              source TEXT NOT NULL, external_id TEXT NOT NULL, title TEXT NOT NULL,
              current_price INTEGER, price_display TEXT NOT NULL, location TEXT,
              url TEXT NOT NULL, description TEXT, first_seen TEXT NOT NULL,
              last_seen TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'new',
              PRIMARY KEY(source, external_id));
            CREATE TABLE price_history (
              id INTEGER PRIMARY KEY, source TEXT NOT NULL, external_id TEXT NOT NULL,
              price INTEGER, observed_at TEXT NOT NULL);
            INSERT INTO listings VALUES (
              'avito','old-1','RTX 3060 12GB',20000,'20 000 ₽','Хабаровск',
              'https://www.avito.ru/old-1',NULL,'2026-01-01T00:00:00+00:00',
              '2026-01-01T00:00:00+00:00','new');
            INSERT INTO price_history(source,external_id,price,observed_at)
              VALUES ('avito','old-1',20000,'2026-01-01T00:00:00+00:00');
        """)
        connection.close()
        before = json.loads(self.config.read_text(encoding="utf-8"))
        with ListingRepository(self.database) as repository:
            self.assertEqual(len(repository.all()), 1)
            self.assertEqual(len(repository.history("avito", "old-1")), 1)
        self.assertEqual(json.loads(self.config.read_text(encoding="utf-8")), before)

    def test_repeated_gui_reads_do_not_write_or_lock(self) -> None:
        self._seed()
        service = GuiService(config_path=self.config, database_path=self.database,
                             output_dir=self.output)
        TrackingConnection.instances.clear()
        try:
            with patch("src.storage.sqlite.sqlite3.connect", side_effect=tracking_connect):
                service.searches()
                service.listings()
                service.dashboard()
                service.source_health()
                service.market_search("RTX")
                service.settings()
        finally:
            service.close()
        writes = [sql for connection in TrackingConnection.instances
                  for sql in write_statements(connection)]
        self.assertEqual(writes, [])

    def test_concurrent_searches_and_listings_reads_do_not_lock(self) -> None:
        self._seed(12)
        service = GuiService(config_path=self.config, database_path=self.database,
                             output_dir=self.output)
        try:
            with ThreadPoolExecutor(max_workers=8) as pool:
                futures = [pool.submit(service.searches if index % 2 else service.listings)
                           for index in range(40)]
                results = [future.result(timeout=5) for future in futures]
            self.assertTrue(all(results))
        finally:
            service.close()

    def test_monitoring_writes_coexist_with_gui_reads(self) -> None:
        self._seed(8)
        service = GuiService(config_path=self.config, database_path=self.database,
                             output_dir=self.output)
        errors: list[BaseException] = []

        def writer() -> None:
            try:
                for cycle in range(10):
                    now = datetime.now(timezone.utc) + timedelta(seconds=cycle)
                    listing = Listing("avito", "0", "RTX 3060 12GB",
                                      20_000 - cycle, f"{20_000-cycle} ₽",
                                      "Хабаровск", "https://www.avito.ru/0")
                    with ListingRepository(self.database) as repository:
                        outcomes = repository.upsert_many([listing], observed_at=now)
                        repository.record_scan_metadata(
                            self.search, [listing], outcomes, [0.0], observed_at=now
                        )
            except BaseException as error:
                errors.append(error)

        thread = threading.Thread(target=writer)
        thread.start()
        try:
            for _ in range(20):
                self.assertTrue(service.listings())
                self.assertEqual(len(service.searches()), 1)
        finally:
            thread.join(timeout=10)
            service.close()
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])

    def test_search_creation_has_no_database_work_after_config_save(self) -> None:
        self._seed()
        service = GuiService(config_path=self.config, database_path=self.database,
                             output_dir=self.output)
        payload = {
            "name": "CPUs", "source": "farpost",
            "url": "https://www.farpost.ru/cpus", "interval_seconds": 900,
        }
        try:
            with patch("src.gui.service.ListingRepository", side_effect=AssertionError("database opened")):
                created = service.create_search(payload)
            self.assertEqual(created["name"], "CPUs")
            self.assertIn("CPUs", [item["name"] for item in json.loads(
                self.config.read_text(encoding="utf-8"))])
        finally:
            service.close()


if __name__ == "__main__":
    unittest.main()
