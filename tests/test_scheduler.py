import unittest

from src.models import SearchConfig
from src.scheduler import SearchScheduler


class SchedulerTests(unittest.TestCase):
    def test_searches_have_independent_due_times(self) -> None:
        short = SearchConfig("short", "x", "https://example/short", interval_seconds=120)
        long = SearchConfig("long", "x", "https://example/long", interval_seconds=600)
        scheduler = SearchScheduler()
        self.assertEqual(scheduler.due([short, long], now=0), [short, long])
        scheduler.mark_scanned(short, now=0)
        scheduler.mark_scanned(long, now=0)
        self.assertEqual(scheduler.due([short, long], now=119), [])
        self.assertEqual(scheduler.due([short, long], now=120), [short])
        scheduler.mark_scanned(short, now=120)
        self.assertEqual(scheduler.due([short, long], now=240), [short])
        self.assertEqual(scheduler.due([short, long], now=599), [short])
        self.assertEqual(scheduler.due([short, long], now=600), [short, long])

    def test_disabled_search_is_never_due(self) -> None:
        search = SearchConfig("off", "x", "https://example/off", enabled=False)
        self.assertEqual(SearchScheduler().due([search], now=0), [])

    def test_jitter_stays_within_configured_boundaries(self) -> None:
        class BoundaryRandom:
            def __init__(self) -> None:
                self.values = iter((-30.0, 30.0))

            def uniform(self, lower: float, upper: float) -> float:
                self.assert_bounds = (lower, upper)
                return next(self.values)

        random_source = BoundaryRandom()
        scheduler = SearchScheduler(random_source=random_source)  # type: ignore[arg-type]
        search = SearchConfig(
            "jittered",
            "avito",
            "https://example/search",
            interval_seconds=300,
            jitter_seconds=30,
        )
        scheduler.mark_scanned(search, now=100)
        self.assertEqual(scheduler.seconds_until_next([search], now=100), 270)
        scheduler.mark_scanned(search, now=100)
        self.assertEqual(scheduler.seconds_until_next([search], now=100), 330)
        self.assertEqual(random_source.assert_bounds, (-30, 30))
