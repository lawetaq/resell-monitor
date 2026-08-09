from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from src.models import Listing, SearchConfig
from src.monitor import Monitor
from src.filtering import matches
from src.sources.base import SearchResult
from src.storage import ListingRepository


def listing(price: int = 1000) -> Listing:
    return Listing("test", "1", "RTX test", price, f"{price} ₽", "Хабаровск", "https://example/1")


class FakeSource:
    def __init__(self, result=None, error=None): self.result, self.error, self.calls = result, error, 0
    def search(self, url, *, debug_dir=None):
        self.calls += 1
        if self.error: raise self.error
        return self.result


class ClassifiedError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool):
        super().__init__(message)
        self.retryable = retryable


class BackendTests(unittest.TestCase):
    def test_broad_category_results_are_filtered_locally(self) -> None:
        config = SearchConfig(
            "broad GPUs",
            "avito",
            "https://www.avito.ru/video",
            include_terms=("rtx", "radeon"),
            exclude_terms=("ремонт",),
            brands=("asus", "gigabyte"),
            min_price=10_000,
            max_price=40_000,
        )
        self.assertTrue(matches(Listing("avito", "1", "ASUS RTX 3060", 25_000, "", None, "u"), config))
        self.assertFalse(matches(Listing("avito", "2", "MSI RTX 3060", 25_000, "", None, "u"), config))
        self.assertFalse(matches(Listing("avito", "3", "ASUS RTX ремонт", 25_000, "", None, "u"), config))
        self.assertFalse(matches(Listing("avito", "4", "ASUS RTX 4090", 100_000, "", None, "u"), config))

    def test_duplicate_and_price_change(self) -> None:
        with TemporaryDirectory() as directory:
            repo = ListingRepository(Path(directory) / "db.sqlite")
            now = datetime(2026, 1, 1, tzinfo=timezone.utc)
            self.assertTrue(repo.upsert(listing(), observed_at=now).is_new)
            self.assertFalse(repo.upsert(listing(), observed_at=now).is_new)
            outcome = repo.upsert(listing(900), observed_at=now)
            self.assertTrue(outcome.price_changed)
            self.assertEqual(outcome.previous_price, 1000)
            self.assertEqual(len(repo.all()), 1)
            self.assertEqual(len(repo.history("test", "1")), 2)
            repo.close()

    def test_source_failure_is_isolated(self) -> None:
        with TemporaryDirectory() as directory:
            repo = ListingRepository(Path(directory) / "db.sqlite")
            monitor = Monitor({"bad": FakeSource(error=RuntimeError("broken")), "good": FakeSource(SearchResult(200, [listing()], "mock", "mock"))}, repo, retries=0)
            scans = monitor.scan([SearchConfig("bad", "bad", "x"), SearchConfig("good", "good", "y")])
            self.assertEqual(scans[0].health.value, "failed")
            self.assertEqual(len(scans[1].events), 1)
            repo.close()

    def test_search_result_rolls_back_atomically_and_next_source_continues(self) -> None:
        with TemporaryDirectory() as directory:
            repo = ListingRepository(Path(directory) / "db.sqlite")
            invalid = listing()
            invalid.title = None  # type: ignore[assignment]
            monitor = Monitor(
                {
                    "bad": FakeSource(SearchResult(200, [listing(), invalid], "mock", "mock")),
                    "good": FakeSource(SearchResult(200, [Listing("good", "2", "Good", 2, "2 ₽", None, "https://example/2")], "mock", "mock")),
                },
                repo,
                retries=0,
            )
            scans = monitor.scan([SearchConfig("bad", "bad", "https://example/bad"), SearchConfig("good", "good", "https://example/good")])
            self.assertEqual(scans[0].health.value, "failed")
            self.assertEqual(scans[1].health.value, "healthy")
            rows = repo.all()
            self.assertEqual([(row["source"], row["external_id"]) for row in rows], [("good", "2")])
            repo.close()

    def test_retries_only_transient_errors(self) -> None:
        with TemporaryDirectory() as directory:
            repo = ListingRepository(Path(directory) / "db.sqlite")
            deterministic = FakeSource(error=ClassifiedError("schema", retryable=False))
            transient = FakeSource(error=ClassifiedError("timeout", retryable=True))
            monitor = Monitor({"det": deterministic, "tmp": transient}, repo, retries=1, cooldown_seconds=0)
            monitor.scan([SearchConfig("det", "det", "https://example/det"), SearchConfig("tmp", "tmp", "https://example/tmp")])
            self.assertEqual(deterministic.calls, 1)
            self.assertEqual(transient.calls, 2)
            repo.close()
