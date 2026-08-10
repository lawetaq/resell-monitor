from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.gui.service import GuiService
from src.retail import RetailPriceObservation
from src.retail_browser import PlaywrightRetailRuntime, RetailBrowserError, RetailBrowserService
from src.retail_browser_adapters import ADAPTERS
from src.retail_browser_models import BrowserNetworkPayload, BrowserPageSnapshot
from src.storage import ListingRepository

KEY = "gpu:rtx-3060:12gb"
FIXTURES = Path(__file__).parent / "fixtures"
DNS_URL = ("https://www.dns-shop.ru/product/891c039318b0cb67/"
           "videokarta-afox-geforce-rtx-3060-af3060-12gd6h4-v4/")
OZON_URL = ("https://www.ozon.ru/product/sinotex-videokarta-geforce-rtx-3060-"
            "sinotex-rtx3060-12-gb-nf306f126f-3551901878/")
WB_URL = "https://www.wildberries.ru/catalog/123456789/detail.aspx"
NOW = datetime(2026, 8, 9, tzinfo=timezone.utc)


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class FakeRuntime:
    def __init__(self, profile: Path, closed, snapshots: dict[str, BrowserPageSnapshot],
                 events: list[object]) -> None:
        self.profile = profile
        self.closed_callback = closed
        self.snapshots = snapshots
        self.events = events
        self.open_pages: dict[str, str] = {}

    def launch(self) -> None:
        self.events.append(("launch", self.profile))

    def navigate(self, retailer: str, url: str) -> dict[str, object]:
        self.events.append(("navigate", retailer, url))
        self.open_pages[retailer] = url
        return {"retailer": retailer, "url": url, "status": 200, "title": retailer}

    def capture(self, retailer: str) -> BrowserPageSnapshot:
        self.events.append(("capture", retailer))
        return self.snapshots[retailer]

    def pages(self) -> list[dict[str, object]]:
        return [{"retailer": key, "url": value, "title": key}
                for key, value in self.open_pages.items()]

    def close(self) -> None:
        self.events.append("close")


def browser_service(root: Path, snapshots: dict[str, BrowserPageSnapshot] | None = None,
                    events: list[object] | None = None) -> RetailBrowserService:
    recorded = events if events is not None else []
    values = snapshots or {}
    return RetailBrowserService(
        profile_root=root / "playwright",
        runtime_factory=lambda profile, closed: FakeRuntime(profile, closed, values, recorded),
        command_timeout=2,
    )


class RetailBrowserLifecycleTests(unittest.TestCase):
    def test_firefox_is_default_and_chromium_is_selectable(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            service = browser_service(Path(root))
            self.assertEqual(service.engine, "firefox")
            self.assertEqual(service.profile_dir.name, "retail-firefox-profile")
            self.assertTrue(service.set_engine("chromium"))
            self.assertEqual(service.profile_dir.name, "retail-chromium-profile")

    def test_explicit_launch_duplicate_launch_and_clean_close(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            events: list[object] = []
            service = browser_service(Path(root), events=events)
            self.assertEqual(service.status().state, "closed")
            self.assertEqual(events, [])
            self.assertTrue(service.open())
            self.assertFalse(service.open())
            self.assertEqual(service.status().state, "open")
            self.assertTrue(service.close())
            self.assertFalse(service.close())
            self.assertEqual(sum(1 for event in events if isinstance(event, tuple)
                                 and event[0] == "launch"), 1)
            self.assertIn("close", events)

    def test_launch_failure_becomes_error_and_can_close_cleanly(self) -> None:
        class BrokenRuntime(FakeRuntime):
            def launch(self) -> None:
                raise RuntimeError("profile locked")
        with tempfile.TemporaryDirectory() as root:
            service = RetailBrowserService(
                profile_root=Path(root) / "playwright",
                runtime_factory=lambda profile, closed: BrokenRuntime(
                    profile, closed, {}, []), command_timeout=2)
            with self.assertRaisesRegex(Exception, "profile locked"):
                service.open()
            self.assertEqual(service.status().state, "error")
            self.assertFalse(service.close())

    def test_profile_isolation_and_reset_only_dedicated_directory(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            service = browser_service(base)
            firefox = base / "playwright" / "retail-firefox-profile"
            chromium = base / "playwright" / "retail-chromium-profile"
            firefox.mkdir(parents=True)
            chromium.mkdir()
            (firefox / "Cookies").write_text("private", encoding="utf-8")
            (chromium / "keep").write_text("yes", encoding="utf-8")
            avito = base / "playwright" / "avito-profile"
            avito.mkdir()
            (avito / "keep").write_text("yes", encoding="utf-8")
            self.assertTrue(service.reset_profile())
            self.assertFalse(firefox.exists())
            self.assertTrue((chromium / "keep").exists())
            self.assertTrue((avito / "keep").exists())
            unsafe = RetailBrowserService(profile_root=base / "not-playwright")
            with self.assertRaises(ValueError):
                unsafe.reset_profile()

    def test_engine_change_requires_closed_browser(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            service = browser_service(Path(root))
            service.open()
            with self.assertRaisesRegex(RetailBrowserError, "Close"):
                service.set_engine("chromium")
            service.close()
            service.set_engine("chromium")
            self.assertEqual(service.status().engine, "chromium")

    def test_missing_firefox_error_is_actionable(self) -> None:
        class MissingBrowser:
            def launch_persistent_context(self, **kwargs):
                raise RuntimeError("Executable doesn't exist")
        class FakePlaywright:
            firefox = MissingBrowser()
            def stop(self) -> None:
                pass
        runtime = PlaywrightRetailRuntime(Path("data/playwright/retail-firefox-profile"),
                                          lambda: None)
        runtime._playwright = FakePlaywright()
        with patch("playwright.sync_api.sync_playwright") as start:
            start.return_value.start.return_value = runtime._playwright
            with self.assertRaisesRegex(
                    RetailBrowserError, "python -m playwright install firefox"):
                runtime.launch()

    def test_navigation_and_capture_are_distinct_explicit_commands(self) -> None:
        snapshot = BrowserPageSnapshot("dns", DNS_URL, "DNS", fixture("dns_product.html"))
        with tempfile.TemporaryDirectory() as root:
            events: list[object] = []
            service = browser_service(Path(root), {"dns": snapshot}, events)
            service.open()
            service.navigate("dns", DNS_URL)
            captured = service.capture("dns")
            service.close()
            self.assertEqual(captured.url, DNS_URL)
            self.assertIn(("navigate", "dns", DNS_URL), events)
            self.assertIn(("capture", "dns"), events)


class RetailBrowserAdapterTests(unittest.TestCase):
    def test_dns_html_capture_prices_identity_and_browser_region(self) -> None:
        result = ADAPTERS["dns"].capture(
            BrowserPageSnapshot("dns", DNS_URL, "DNS", fixture("dns_product.html")),
            KEY, DNS_URL, confirmed_region="Khabarovsk",
        )
        self.assertEqual(result.status, "captured")
        row = result.observations[0]
        self.assertEqual((row.price, row.product_id), (31999, "9279718"))
        self.assertEqual(row.region, "Khabarovsk; source=browser-confirmed")
        self.assertEqual(row.retrieval_method, "browser-assisted")

    def test_ozon_embedded_capture_keeps_conditional_price_and_seller(self) -> None:
        result = ADAPTERS["ozon"].capture(
            BrowserPageSnapshot("ozon", OZON_URL, "Ozon", fixture("ozon_product.html")),
            KEY, OZON_URL,
        )
        self.assertEqual(result.status, "captured")
        row = result.observations[0]
        self.assertEqual((row.price, row.conditional_price), (31990, 29490))
        self.assertEqual(row.seller, "Computer Shop")
        self.assertEqual(row.region, "default-unresolved; source=browser")

    def test_wildberries_natural_network_capture_variants_and_destination(self) -> None:
        payload = json.loads(fixture("wildberries_card.json"))
        product = payload["products"][0] if "products" in payload else payload["data"]["products"][0]
        product["id"] = 123456789
        network = BrowserNetworkPayload(
            "https://card.wb.ru/cards/v4/detail?nm=123456789&dest=-123456",
            200, "application/json", payload,
        )
        result = ADAPTERS["wildberries"].capture(
            BrowserPageSnapshot("wildberries", WB_URL, "Wildberries", "<html></html>", (network,)),
            KEY, WB_URL, confirmed_region="Khabarovsk",
        )
        self.assertEqual(result.status, "captured")
        self.assertTrue(result.observations)
        self.assertIn("wb_dest=-123456", result.region_context)
        self.assertTrue(all(row.product_id == "123456789" for row in result.observations))

    def test_identity_mismatch_and_challenge_never_capture(self) -> None:
        mismatch = ADAPTERS["ozon"].capture(
            BrowserPageSnapshot("ozon", OZON_URL.replace("3551901878", "9999999999"),
                                "Ozon", fixture("ozon_product.html")), KEY, OZON_URL)
        self.assertEqual(mismatch.status, "identity_mismatch")
        self.assertFalse(mismatch.observations)
        challenge = ADAPTERS["dns"].capture(
            BrowserPageSnapshot("dns", DNS_URL, "Qrator", "<html>Qrator challenge</html>"),
            KEY, DNS_URL)
        self.assertEqual(challenge.status, "challenge")
        self.assertFalse(challenge.observations)

    def test_strict_title_mismatch_does_not_store_price(self) -> None:
        html = fixture("dns_product.html").replace("RTX 3060", "RTX 3060 Ti")
        result = ADAPTERS["dns"].capture(
            BrowserPageSnapshot("dns", DNS_URL, "DNS", html), KEY, DNS_URL)
        self.assertEqual(result.status, "no_reliable_match")
        self.assertFalse(result.observations)


class RetailBrowserGuiAndStorageTests(unittest.TestCase):
    def make_service(self, root: Path, browser: RetailBrowserService) -> GuiService:
        return GuiService(config_path=root / "searches.json",
                          database_path=root / "data" / "x.db",
                          output_dir=root / "output", retail_browser=browser)

    def test_mapping_create_edit_delete_and_host_validation(self) -> None:
        with tempfile.TemporaryDirectory() as root_value:
            root = Path(root_value)
            service = self.make_service(root, browser_service(root))
            saved = service.save_retail_mapping(KEY, "dns", DNS_URL)
            self.assertEqual((saved["validation"], saved["product_id"]),
                             ("valid", "891c039318b0cb67"))
            edited_url = DNS_URL.replace("891c039318b0cb67", "0123456789abcdef")
            edited = service.save_retail_mapping(KEY, "dns", edited_url)
            self.assertEqual(edited["product_id"], "0123456789abcdef")
            with self.assertRaises(ValueError):
                service.save_retail_mapping(KEY, "ozon", DNS_URL)
            service.delete_retail_mapping(KEY, "dns")
            self.assertEqual(service.retail_mappings(KEY)[0]["validation"], "unmapped")
            service.close()

    def test_capture_persists_method_and_keeps_http_health_separate(self) -> None:
        snapshot = BrowserPageSnapshot("dns", DNS_URL, "DNS", fixture("dns_product.html"))
        with tempfile.TemporaryDirectory() as root_value:
            root = Path(root_value)
            browser = browser_service(root, {"dns": snapshot})
            service = self.make_service(root, browser)
            service.save_retail_mapping(KEY, "dns", DNS_URL)
            with ListingRepository(service.database_path) as repository:
                repository.record_retail_provider_state(
                    "dns", health="blocked", successful=False, status=401,
                    transport="requests-persistent", error="Qrator", observed_at=NOW,
                    next_refresh_at=NOW + timedelta(hours=12), region="default",
                    retrieval_method="mapped_product", block_classification="challenge")
            service.open_retail_browser()
            service.open_retail_mapping(KEY, "dns")
            result = service.capture_retail_mapping(KEY, "dns", confirmed_region="Khabarovsk")
            self.assertEqual(result["status"], "captured")
            with ListingRepository(service.database_path) as repository:
                observation = repository.latest_retail_observation(
                    KEY, "dns", retrieval_method="browser-assisted")
            self.assertEqual(observation["retrieval_method"], "browser-assisted")
            health = service.retail_health()[0]
            self.assertEqual(health["method_states"]["http"]["last_http_status"], 401)
            self.assertEqual(health["method_states"]["browser-assisted"]["health"], "healthy")
            service.close()

    def test_market_reads_never_construct_or_launch_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as root_value:
            root = Path(root_value)
            events: list[object] = []
            service = self.make_service(root, browser_service(root, events=events))
            self.assertEqual(service.market_search("RTX 3060"), [])
            self.assertEqual(service.retail_browser_status()["state"], "closed")
            self.assertEqual(events, [])
            service.close()

    def test_reset_requires_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as root_value:
            root = Path(root_value)
            service = self.make_service(root, browser_service(root))
            with self.assertRaises(ValueError):
                service.reset_retail_browser_profile(confirmed=False)
            service.close()

    def test_v5_migration_preserves_observations_and_runs_once(self) -> None:
        with tempfile.TemporaryDirectory() as root_value:
            database = Path(root_value) / "legacy.db"
            with ListingRepository(database) as repository:
                repository.add_retail_observation(RetailPriceObservation(
                    KEY, "dns", 31999, NOW, product_title="RTX 3060 12GB",
                    availability="available", match_confidence="exact"))
            connection = sqlite3.connect(database)
            try:
                connection.execute("DROP TABLE retail_provider_method_state")
                connection.execute(
                    "ALTER TABLE retail_price_observations DROP COLUMN retrieval_method")
                connection.execute("PRAGMA user_version=5")
                connection.commit()
            finally:
                connection.close()
            with ListingRepository(database) as repository:
                row = repository.retail_observations(KEY)[0]
                self.assertEqual(row["retrieval_method"], "http")
                self.assertEqual(repository.retail_provider_method_states(), [])
            before = database.stat().st_mtime_ns
            with ListingRepository(database):
                pass
            self.assertEqual(database.stat().st_mtime_ns, before)


if __name__ == "__main__":
    unittest.main()
