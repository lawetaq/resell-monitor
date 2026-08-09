from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.analytics import assess_resale, normalize_product
from src.gui.service import GuiService
from src.debug_retail import _safe_url, _sanitize
from src.retail import RetailPriceObservation, aggregate_retail_offers
from src.retail_monitor import RetailMonitor
from src.retail_providers import parse_dns, parse_ozon, parse_wildberries
from src.retail_providers.common import exact_match
from src.sources.http import HttpPage
from src.storage import ListingRepository


KEY = "gpu:rtx-3060:12gb"
NOW = datetime(2026, 8, 8, tzinfo=timezone.utc)


class RetailParserTests(unittest.TestCase):
    def test_diagnostic_artifacts_remove_secrets(self) -> None:
        self.assertEqual(_safe_url("https://shop.test/x?token=secret"), "https://shop.test/x")
        cleaned = _sanitize('access_token="secret" https://user:pass@shop.test/x')
        self.assertNotIn("secret", cleaned)
        self.assertNotIn("pass", cleaned)

    def test_dns_html_and_exact_matching(self) -> None:
        html = '<script type="application/ld+json">'+json.dumps({
            "@type":"Product","name":"Видеокарта Palit RTX 3060 12GB",
            "sku":"42","url":"/product/42","offers":{"price":"24990","availability":"https://schema.org/InStock"}})+'</script>'
        rows = parse_dns(html)
        self.assertEqual(rows[0]["price"], "24990")
        self.assertTrue(exact_match(KEY, str(rows[0]["title"]))[0])

    def test_ozon_embedded_widget_multiple_offers(self) -> None:
        data = {"widgetStates":{"search":json.dumps({"items":[
            {"id":"1","title":"RTX 3060 12GB MSI","finalPrice":"23 790 ₽","sellerName":"A"},
            {"id":"2","title":"RTX 3060 12GB Palit","finalPrice":"24 300 ₽","sellerName":"B"}]})}}
        rows = parse_ozon('<script type="application/json">'+json.dumps(data)+'</script>')
        self.assertEqual(sorted(x["price"] for x in rows), [23790, 24300])

    def test_wildberries_json_region_offer(self) -> None:
        body = json.dumps({"data":{"products":[{"id":7,"name":"RTX 3060 12GB Gigabyte",
            "sizes":[{"price":{"product":2410000}}],"totalQuantity":3,"supplier":"Shop"}]}})
        row = parse_wildberries(body)[0]
        self.assertEqual(row["price"], 24100)
        self.assertEqual(row["seller"], "Shop")

    def test_wrong_models_are_rejected(self) -> None:
        for title in ("RTX 3060 8GB", "RTX 3060 Ti 12GB", "Ноутбук RTX 3060 12GB"):
            self.assertFalse(exact_match(KEY, title)[0])
        self.assertNotEqual(normalize_product("RX 6600").comparable_key,
                            normalize_product("RX 6600 XT").comparable_key)


class RetailStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.repo = ListingRepository(Path(self.temp.name)/"retail.sqlite")

    def tearDown(self) -> None:
        self.repo.close(); self.temp.cleanup()

    def observation(self, price: int, *, retailer: str = "ozon", offer: str = "1",
                    at: datetime = NOW, region: str = "Khabarovsk") -> RetailPriceObservation:
        return RetailPriceObservation(KEY, retailer, price, at,
            product_title="RTX 3060 12GB", availability="available", region=region,
            match_confidence="exact", offer_id=offer)

    def test_duplicate_state_updates_last_seen_and_price_change_keeps_history(self) -> None:
        self.repo.add_retail_observation(self.observation(24000))
        self.repo.add_retail_observation(self.observation(24000, at=NOW+timedelta(hours=1)))
        self.repo.add_retail_observation(self.observation(23000, at=NOW+timedelta(days=1)))
        rows = self.repo.retail_observations(KEY)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["last_seen_at"], (NOW+timedelta(hours=1)).isoformat())

    def test_multiple_offers_representative_median_and_region(self) -> None:
        for price, offer in ((20000,"a"),(24000,"b"),(25000,"c")):
            self.repo.add_retail_observation(self.observation(price, offer=offer))
        summary = self.repo.retail_summary(KEY, now=NOW)
        self.assertEqual(summary["representative_price"], 24000)
        self.assertEqual(summary["cheapest_price"], 20000)
        self.assertEqual(summary["offer_count"], 3)
        self.assertEqual(self.repo.retail_observations(KEY)[0]["region"], "Khabarovsk")

    def test_retail_history_ranges_and_stale_age(self) -> None:
        self.repo.add_retail_observation(self.observation(24000, at=NOW-timedelta(days=40)))
        self.repo.add_retail_observation(self.observation(23000, offer="2", at=NOW-timedelta(days=2)))
        self.assertEqual(len(self.repo.retail_history(KEY, days=7, now=NOW)), 1)
        self.assertEqual(len(self.repo.retail_history(KEY, days=None, now=NOW)), 2)
        stale = self.repo.retail_summary(KEY, stale_hours=1, now=NOW)
        self.assertTrue(stale["retailers"][0]["stale"])

    def test_mapping_and_provider_health_storage(self) -> None:
        self.repo.set_retail_mapping(KEY,"dns","https://www.dns-shop.ru/product/42")
        self.repo.record_retail_provider_state("dns",health="degraded",successful=False,
            status=401,transport="requests-persistent",error="challenge",observed_at=NOW,
            next_refresh_at=NOW+timedelta(hours=12),region="Khabarovsk")
        self.assertEqual(self.repo.retail_mappings(KEY)[0]["product_url"], "https://www.dns-shop.ru/product/42")
        self.assertEqual(self.repo.retail_provider_states()[0]["health"], "degraded")


class RetailRankingTests(unittest.TestCase):
    def base(self, **extra):
        return assess_resale(asking_price=14000, median=19000, q1=17500,
            sample_count=12, first_seen=NOW, activity_count=5, **extra)

    def test_ranking_without_retail_is_unchanged_and_noisy_offer_is_ignored(self) -> None:
        plain = self.base()
        noisy = self.base(retail_price=100000, retail_confidence="low")
        self.assertEqual(plain.score, noisy.score)

    def test_confident_retail_can_strengthen_or_weaken_only_slightly(self) -> None:
        plain = self.base()
        strong = self.base(retail_price=26000, retail_confidence="high")
        weak = assess_resale(asking_price=18000,median=19000,q1=17500,sample_count=12,
            first_seen=NOW,retail_price=19900,retail_confidence="high")
        weak_plain = assess_resale(asking_price=18000,median=19000,q1=17500,sample_count=12,first_seen=NOW)
        self.assertGreaterEqual(strong.score, plain.score)
        self.assertLessEqual(abs(strong.score-plain.score), 4)
        self.assertLess(weak.score, weak_plain.score)
        self.assertIsNotNone(strong.listing_discount_vs_retail)

    def test_offer_aggregate_requires_credible_matches(self) -> None:
        offers=[RetailPriceObservation(KEY,"x",x,NOW,availability="available",match_confidence="exact") for x in (20,30,100)]
        self.assertEqual(aggregate_retail_offers(offers).representative_price,30)


class RetailIsolationAndGuiTests(unittest.TestCase):
    def test_provider_failure_isolation(self) -> None:
        class Provider:
            def __init__(self,name,fail=False): self.name=name; self.fail=fail
            def search(self,*a,**k):
                if self.fail: raise RuntimeError("blocked")
                from src.retail import RetailRetrievalResult
                return RetailRetrievalResult(self.name,(RetailPriceObservation(KEY,self.name,24000,NOW,availability="available",match_confidence="exact"),),200)
            def close(self): pass
        with tempfile.TemporaryDirectory() as root, ListingRepository(Path(root)/"x.db") as repo:
            rows=RetailMonitor({"bad":Provider("bad",True),"good":Provider("good")},repo).refresh(KEY,"RTX 3060 12GB")
            self.assertEqual([x.health for x in rows],["failed","healthy"])
            self.assertEqual(len(repo.retail_observations(KEY)),1)

    def test_market_reads_do_not_request_and_manual_refresh_is_background(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            called=[]
            service=GuiService(config_path=Path(root)/"searches.json",database_path=Path(root)/"x.db",output_dir=Path(root)/"out",scan_runner=lambda *_: called.append(1))
            self.assertEqual(service.market_search("RTX 3060"),[])
            self.assertEqual(called,[])
            # Disabled providers make the worker finish without any network client.
            started=service.trigger_retail_refresh(KEY,"RTX 3060 12GB")
            self.assertTrue(started)
            deadline=time.time()+2
            while service._retail_lock.locked() and time.time()<deadline: time.sleep(.01)
            self.assertEqual(called,[])
            service.close()


if __name__ == "__main__": unittest.main()
