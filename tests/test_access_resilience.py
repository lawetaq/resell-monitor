from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from src.models import Listing, SearchConfig
from src.monitor import Monitor
from src.sources.avito import AvitoError
from src.sources.base import HealthState, SearchResult
from src.storage import ListingRepository


def avito_listing(identifier: str = "1", title: str = "ASUS RTX 3060") -> Listing:
    return Listing(
        "avito",
        identifier,
        title,
        25_000,
        "25 000 ₽",
        "Хабаровск",
        f"https://www.avito.ru/{identifier}",
    )


def success(identifier: str = "1") -> SearchResult:
    return SearchResult(
        200,
        [avito_listing(identifier)],
        "curl_cffi",
        "embedded-json",
        impersonation="chrome",
        session_mode="persistent",
        response_cookie_names=("srv_id", "u"),
    )


def blocked(status: int) -> AvitoError:
    return AvitoError(
        f"HTTP {status}",
        status_code=status,
        transport="curl_cffi",
        impersonation="chrome",
        session_mode="persistent",
        response_cookie_names=("srv_id",),
        block_classification="http-block",
    )


class SequenceSource:
    def __init__(self, outcomes: list[SearchResult | Exception], order=None) -> None:
        self.outcomes = outcomes
        self.calls = 0
        self.order = order

    def search(self, url: str, *, debug_dir=None) -> SearchResult:
        self.calls += 1
        if self.order is not None:
            self.order.append(url)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class AccessResilienceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.repository = ListingRepository(Path(self.temporary.name) / "state.sqlite")
        self.now = datetime(2026, 8, 8, 6, 0, tzinfo=timezone.utc)
        self.sleeps: list[float] = []
        self.search = SearchConfig(
            "broad GPUs",
            "avito",
            "https://www.avito.ru/gpus",
            block_retry_delay_seconds=45,
            block_cooldown_seconds=600,
        )

    def tearDown(self) -> None:
        self.repository.close()
        self.temporary.cleanup()

    def monitor(self, source: SequenceSource) -> Monitor:
        return Monitor(
            {"avito": source},
            self.repository,
            retries=0,
            sleep=self.sleeps.append,
            clock=lambda: self.now,
        )

    def test_403_followed_by_one_delayed_successful_retry(self) -> None:
        source = SequenceSource([blocked(403), success()])
        scan = self.monitor(source).scan([self.search])[0]

        self.assertEqual(source.calls, 2)
        self.assertEqual(self.sleeps, [45])
        self.assertEqual(scan.health, HealthState.HEALTHY)
        self.assertEqual(scan.access_state.consecutive_blocks, 0)  # type: ignore[union-attr]
        attempts = self.repository.source_attempts("avito", "broad GPUs")
        self.assertEqual([row["http_status"] for row in attempts], [403, 200])
        self.assertEqual(attempts[0]["block_classification"], "http-block")
        self.assertEqual(attempts[1]["response_cookie_names"], '["srv_id", "u"]')

    def test_429_followed_by_one_delayed_successful_retry(self) -> None:
        source = SequenceSource([blocked(429), success()])
        scan = self.monitor(source).scan([self.search])[0]

        self.assertEqual(source.calls, 2)
        self.assertEqual(scan.health, HealthState.HEALTHY)
        self.assertEqual(scan.access_state.last_http_status, 200)  # type: ignore[union-attr]

    def test_two_blocked_attempts_enter_cooldown(self) -> None:
        source = SequenceSource([blocked(403), blocked(429)])
        scan = self.monitor(source).scan([self.search])[0]

        self.assertEqual(source.calls, 2)
        self.assertEqual(scan.health, HealthState.COOLDOWN)
        self.assertEqual(scan.access_state.consecutive_blocks, 2)  # type: ignore[union-attr]
        self.assertEqual(scan.access_state.blocked_until, self.now + timedelta(seconds=600))  # type: ignore[union-attr]

        again = self.monitor(source).scan([self.search])[0]
        self.assertEqual(again.health, HealthState.COOLDOWN)
        self.assertEqual(source.calls, 2)

    def test_successful_scan_recovers_after_cooldown(self) -> None:
        source = SequenceSource([blocked(403), blocked(429), success()])
        monitor = self.monitor(source)
        monitor.scan([self.search])
        self.now += timedelta(seconds=601)

        recovered = monitor.scan([self.search])[0]

        self.assertEqual(recovered.health, HealthState.HEALTHY)
        self.assertEqual(recovered.access_state.consecutive_blocks, 0)  # type: ignore[union-attr]
        self.assertIsNone(recovered.access_state.blocked_until)  # type: ignore[union-attr]
        self.assertEqual(recovered.access_state.last_success_at, self.now)  # type: ignore[union-attr]

    def test_searches_execute_sequentially_in_configuration_order(self) -> None:
        order: list[str] = []
        first = SequenceSource([success("1")], order)
        second = SequenceSource([success("2")], order)
        monitor = Monitor(
            {"first": first, "second": second},
            self.repository,
            retries=0,
            clock=lambda: self.now,
        )
        searches = [
            SearchConfig("one", "first", "https://example/one"),
            SearchConfig("two", "second", "https://example/two"),
        ]

        monitor.scan(searches)

        self.assertEqual(order, ["https://example/one", "https://example/two"])

    def test_sqlite_deduplicates_repeated_broad_search_results(self) -> None:
        source = SequenceSource([success(), success()])
        monitor = self.monitor(source)

        first = monitor.scan([self.search])[0]
        second = monitor.scan([self.search])[0]

        self.assertTrue(first.events[0].outcome.is_new)
        self.assertFalse(second.events[0].outcome.is_new)
        self.assertFalse(second.events[0].outcome.price_changed)
        self.assertEqual(len(self.repository.all()), 1)
        self.assertEqual(len(self.repository.history("avito", "1")), 1)


if __name__ == "__main__":
    unittest.main()
