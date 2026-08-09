from __future__ import annotations

import time
import random
from collections.abc import Iterable

from src.models import SearchConfig

SearchKey = tuple[str, str, str]


def search_key(search: SearchConfig) -> SearchKey:
    return search.source, search.name, search.url


class SearchScheduler:
    """Tracks independent due times using a monotonic clock."""

    def __init__(self, *, random_source: random.Random | None = None) -> None:
        self._next_due: dict[SearchKey, float] = {}
        self._random = random_source or random.Random()

    def due(
        self,
        searches: Iterable[SearchConfig],
        *,
        now: float | None = None,
    ) -> list[SearchConfig]:
        current = time.monotonic() if now is None else now
        return [
            search
            for search in searches
            if search.enabled and current >= self._next_due.get(search_key(search), 0.0)
        ]

    def mark_scanned(self, search: SearchConfig, *, now: float | None = None) -> None:
        current = time.monotonic() if now is None else now
        jitter = self._random.uniform(-search.jitter_seconds, search.jitter_seconds)
        self._next_due[search_key(search)] = current + search.interval_seconds + jitter

    def seconds_until_next(
        self,
        searches: Iterable[SearchConfig],
        *,
        now: float | None = None,
    ) -> float | None:
        current = time.monotonic() if now is None else now
        enabled = list(search for search in searches if search.enabled)
        if not enabled:
            return None
        waits = [max(0.0, self._next_due.get(search_key(search), 0.0) - current) for search in enabled]
        return min(waits)
