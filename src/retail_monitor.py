from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from src.retail import RetailPriceProvider, RetailRetrievalResult
from src.storage import ListingRepository


@dataclass(slots=True, frozen=True)
class RetailRefresh:
    retailer: str
    health: str
    candidates: int
    accepted: int
    error: str | None = None
    retrieval_method: str = "search"
    block_classification: str = "none"


class RetailMonitor:
    """Sequential, failure-isolated retail refresh orchestration."""

    def __init__(self, providers: dict[str, RetailPriceProvider], repository: ListingRepository,
                 *, interval: timedelta = timedelta(hours=12)) -> None:
        self.providers = providers
        self.repository = repository
        self.interval = interval

    def refresh(self, comparable_key: str, query: str, *, region: str | None = None,
                mappings: dict[str, str] | None = None,
                progress: Callable[[str], None] | None = None) -> list[RetailRefresh]:
        output: list[RetailRefresh] = []
        for name, provider in self.providers.items():
            if progress: progress(f"Refreshing {name.title()} retail…")
            now = datetime.now(timezone.utc)
            try:
                result = provider.search(comparable_key, query, region=region,
                                         mapped_url=(mappings or {}).get(name))
                for observation in result.observations:
                    self.repository.add_retail_observation(observation)
                successful = result.health == "healthy"
                error = result.error
            except Exception as exc:
                result = RetailRetrievalResult(name, (), health="failed", error=str(exc))
                successful = False
                error = str(exc)
            cooldown = self.interval
            if result.retry_after_seconds is not None:
                cooldown = max(cooldown, timedelta(seconds=result.retry_after_seconds))
            self.repository.record_retail_provider_state(
                name, health=result.health, successful=successful,
                status=result.status_code, transport=result.transport, error=error,
                observed_at=now, next_refresh_at=now + cooldown,
                region=result.region_context or region,
                retrieval_method=result.retrieval_method,
                last_observation_at=now if result.observations else None,
                block_classification=result.block_classification,
                retry_after_seconds=result.retry_after_seconds,
                response_content_type=result.response_content_type,
                response_size=result.response_size)
            output.append(RetailRefresh(name, result.health, result.candidates_found,
                                        len(result.observations), error,
                                        result.retrieval_method,
                                        result.block_classification))
        return output

    def close(self) -> None:
        for provider in self.providers.values():
            provider.close()


class RetailScheduler:
    def __init__(self, interval: timedelta = timedelta(hours=12)) -> None:
        self.interval = interval

    def due(self, last_refresh: datetime | None, *, now: datetime | None = None) -> bool:
        return last_refresh is None or (now or datetime.now(timezone.utc)) >= last_refresh + self.interval
