from __future__ import annotations

import json
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from src.config import save_searches
from src.gui.service import GuiService
from src.models import Listing, SearchConfig
from src.monitor import SourceScan
from src.sources.base import HealthState
from src.storage import ListingRepository


class RecordingRunner:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.called = threading.Event()

    def __call__(self, searches, progress):
        self.calls.append([search.name for search in searches])
        for search in searches:
            progress(f"Scanning {search.source.title()}…")
        self.called.set()
        return [SourceScan(search, HealthState.HEALTHY) for search in searches]


class GuiServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.config = self.root / "searches.json"
        self.database = self.root / "monitor.sqlite"
        self.output = self.root / "output"
        self.runner = RecordingRunner()
        save_searches(
            self.config,
            [
                SearchConfig(
                    "GPUs",
                    "avito",
                    "https://www.avito.ru/gpus",
                    interval_seconds=600,
                    proxy_url="http://user:secret@127.0.0.1:8080",
                    network_route="proxy",
                )
            ],
        )
        self.service = GuiService(
            config_path=self.config,
            database_path=self.database,
            output_dir=self.output,
            scan_runner=self.runner,
        )

    def tearDown(self) -> None:
        self.service.close()
        self.temporary.cleanup()

    def test_startup_and_read_views_make_no_marketplace_call(self) -> None:
        self.service.dashboard()
        self.service.searches()
        self.service.listings()
        self.service.source_health()
        self.assertEqual(self.runner.calls, [])
        self.assertEqual(self.service.runtime()["state"], "Idle")

    def test_export_generation_makes_no_marketplace_call_and_does_not_overwrite(self) -> None:
        first = self.service.export("json")
        second = self.service.export("json")
        self.assertNotEqual(first, second)
        self.assertTrue(first.exists())
        self.assertTrue(second.exists())
        self.assertEqual(self.runner.calls, [])

    def test_search_create_edit_enable_disable_delete_and_secret_redaction(self) -> None:
        public = self.service.searches()[0]
        self.assertTrue(public["proxy_configured"])
        self.assertNotIn("proxy_url", public)
        self.assertNotIn("secret", json.dumps(public, default=str))

        created = self.service.create_search(
            {
                "name": "CPUs",
                "source": "farpost",
                "url": "https://www.farpost.ru/cpus",
                "interval_seconds": 900,
                "include_terms": ["ryzen"],
            }
        )
        self.assertEqual(created["name"], "CPUs")
        disabled = self.service.set_search_enabled("CPUs", False)
        self.assertFalse(disabled["enabled"])

        payload = {
            key: value
            for key, value in self.service.searches(include_secrets=True)[0].items()
            if key
            not in {
                "proxy_configured",
                "health",
                "last_result_count",
                "last_success_at",
                "last_status",
                "blocked_until",
                "next_scan",
            }
        }
        payload.pop("proxy_url")
        payload["name"] = "Graphics"
        updated = self.service.update_search("GPUs", payload)
        self.assertEqual(updated["name"], "Graphics")
        saved = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertIn("user:secret", saved[0]["proxy_url"])

        self.service.delete_search("CPUs")
        self.assertEqual([item["name"] for item in self.service.searches()], ["Graphics"])

    def test_search_configuration_validation_is_reused(self) -> None:
        with self.assertRaisesRegex(ValueError, "interval_seconds"):
            self.service.create_search(
                {
                    "name": "bad",
                    "source": "avito",
                    "url": "https://www.avito.ru/x",
                    "interval_seconds": 10,
                }
            )
        with self.assertRaisesRegex(ValueError, "requires proxy_url"):
            self.service.create_search(
                {
                    "name": "bad proxy",
                    "source": "avito",
                    "url": "https://www.avito.ru/x",
                    "network_route": "proxy",
                }
            )

    def test_global_settings_persist_and_validate(self) -> None:
        updated = self.service.update_settings(
            {
                "default_interval_seconds": 1200,
                "default_jitter_seconds": 45,
                "default_export_format": "html",
                "ui_refresh_seconds": 4,
            }
        )
        self.assertEqual(updated["default_interval_seconds"], 1200)
        self.assertEqual(updated["default_export_format"], "html")
        self.assertEqual(self.service.settings()["ui_refresh_seconds"], 4)
        self.assertEqual(self.service.update_settings({"interface_language": "ru"})["interface_language"], "ru")
        themed = self.service.update_settings({"appearance_mode": "dark", "color_theme": "moss"})
        self.assertEqual((themed["appearance_mode"], themed["color_theme"]), ("dark", "moss"))

        with self.assertRaisesRegex(ValueError, "default interval"):
            self.service.update_settings({"default_interval_seconds": 30})
        with self.assertRaisesRegex(ValueError, "default export format"):
            self.service.update_settings({"default_export_format": "csv"})
        with self.assertRaisesRegex(ValueError, "interface language"):
            self.service.update_settings({"interface_language": "de"})
        with self.assertRaisesRegex(ValueError, "appearance mode"):
            self.service.update_settings({"appearance_mode": "automatic"})
        with self.assertRaisesRegex(ValueError, "color theme"):
            self.service.update_settings({"color_theme": "neon"})

    def test_listing_status_detail_history_export_and_copy(self) -> None:
        now = datetime(2026, 8, 8, tzinfo=timezone.utc)
        listing = Listing(
            "avito",
            "1",
            "ASUS RTX 3060",
            25_000,
            "25 000 ₽",
            "Хабаровск",
            "https://www.avito.ru/1",
        )
        search = SearchConfig("GPUs", "avito", "https://www.avito.ru/gpus")
        with ListingRepository(self.database) as repository:
            first = repository.upsert_many([listing], observed_at=now)
            repository.record_scan_metadata(
                search, [listing], first, [12.5], observed_at=now
            )
            cheaper = Listing(
                "avito",
                "1",
                listing.title,
                23_000,
                "23 000 ₽",
                listing.location,
                listing.url,
            )
            second = repository.upsert_many(
                [cheaper], observed_at=now + timedelta(minutes=5)
            )
            repository.record_scan_metadata(
                search,
                [cheaper],
                second,
                [20.0],
                observed_at=now + timedelta(minutes=5),
            )

        changed = self.service.set_listing_status("avito", "1", "interesting")
        self.assertEqual(changed["status"], "interesting")
        self.assertEqual(changed["previous_price"], 25_000)
        self.assertEqual(len(changed["price_history"]), 2)
        self.assertEqual(self.service.listings({"price_drops": True})[0]["external_id"], "1")
        self.assertEqual(self.service.history()[0]["event_type"], "price_drop")

        text = self.service.copy_for_analysis()
        self.assertIn("ASUS RTX 3060", text)
        self.assertIn("previous 25000 ₽", text)
        for format_name in ("json", "txt", "html"):
            self.assertTrue(self.service.export(format_name).exists())

    def test_health_presentation_includes_cooldown_and_youla_experimental(self) -> None:
        now = datetime.now(timezone.utc)
        search = SearchConfig(
            "GPUs",
            "avito",
            "https://www.avito.ru/gpus",
            block_cooldown_seconds=600,
        )
        with ListingRepository(self.database) as repository:
            repository.record_search_block(
                search,
                status=429,
                transport="curl_cffi",
                observed_at=now,
                enter_cooldown=True,
            )
        health = {item["source"]: item for item in self.service.source_health()}
        self.assertEqual(health["avito"]["health"], "cooldown")
        self.assertIsNotNone(health["avito"]["blocked_until"])
        self.assertEqual(health["youla"]["health"], "experimental")
        self.assertNotIn("secret", json.dumps(health, default=str))

    def test_manual_scan_trigger_and_monitoring_start_stop(self) -> None:
        scans = self.service.scan_now("GPUs")
        self.assertEqual(len(scans), 1)
        self.assertEqual(self.runner.calls, [["GPUs"]])

        self.runner.called.clear()
        self.assertTrue(self.service.start_monitoring())
        self.assertTrue(self.runner.called.wait(timeout=2))
        self.assertTrue(self.service.runtime()["monitoring"])
        self.assertTrue(self.service.stop_monitoring())
        self.assertFalse(self.service.runtime()["monitoring"])


if __name__ == "__main__":
    unittest.main()
