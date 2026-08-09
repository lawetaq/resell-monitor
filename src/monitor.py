from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping

from src.filtering import matches
from src.models import Listing, SearchConfig
from src.sources.base import HealthState, MarketplaceSource, SearchResult
from src.storage.sqlite import ListingRepository, SearchAccessState, UpsertOutcome

LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class ScanEvent:
    listing: Listing
    outcome: UpsertOutcome
    score: float


@dataclass(slots=True)
class SourceScan:
    search: SearchConfig
    health: HealthState
    events: list[ScanEvent] = field(default_factory=list)
    error: str | None = None
    result: SearchResult | None = None
    access_state: SearchAccessState | None = None


class Monitor:
    def __init__(
        self,
        sources: Mapping[object, MarketplaceSource],
        repository: ListingRepository,
        *,
        debug_dir: Path | None = None,
        retries: int = 0,
        cooldown_seconds: float = 2,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        if retries < 0 or cooldown_seconds < 0:
            raise ValueError("retries and cooldown_seconds must be non-negative")
        self.sources = sources
        self.repository = repository
        self.debug_dir = debug_dir
        self.retries = retries
        self.cooldown_seconds = cooldown_seconds
        self.sleep = sleep
        self.clock = clock

    def scan(self, searches: list[SearchConfig]) -> list[SourceScan]:
        scans: list[SourceScan] = []
        for config in searches:
            if not config.enabled:
                continue
            source = self.sources.get((config.source, config.name)) or self.sources.get(
                config.source
            )
            if source is None:
                scans.append(SourceScan(config, HealthState.FAILED, error=f"unknown source: {config.source}"))
                continue
            access_state = self.repository.search_access_state(config)
            now = self.clock()
            if access_state.blocked_until and now < access_state.blocked_until:
                scans.append(
                    SourceScan(
                        config,
                        HealthState.COOLDOWN,
                        error=f"cooldown until {access_state.blocked_until.isoformat()}",
                        access_state=access_state,
                    )
                )
                continue
            error: Exception | None = None
            result: SearchResult | None = None
            transient_retries = 0
            block_retries = 0
            block_outcome_recorded = False
            while True:
                attempted_at = self.clock()
                try:
                    result = source.search(config.url, debug_dir=self.debug_dir)
                    self._record_attempt(config, attempted_at, result=result)
                    break
                except Exception as caught:  # source boundary deliberately isolates adapters
                    error = caught
                    self._record_attempt(config, attempted_at, error=caught)
                    LOGGER.warning("%s scan failed: %s", config.name, caught)
                    if _is_explicit_block(caught):
                        will_retry = block_retries < config.max_block_retries
                        access_state = self.repository.record_search_block(
                            config,
                            status=getattr(caught, "status_code"),
                            transport=getattr(caught, "transport", None),
                            observed_at=attempted_at,
                            enter_cooldown=not will_retry and block_retries > 0,
                        )
                        if will_retry:
                            block_retries += 1
                            self.sleep(config.block_retry_delay_seconds)
                            continue
                        scans.append(
                            SourceScan(
                                config,
                                HealthState.COOLDOWN
                                if access_state.blocked_until
                                else HealthState.TEMPORARILY_BLOCKED,
                                error=str(caught),
                                access_state=access_state,
                            )
                        )
                        block_outcome_recorded = True
                        break
                    access_state = self.repository.record_search_failure(
                        config,
                        status=getattr(caught, "status_code", None),
                        transport=getattr(caught, "transport", None),
                        observed_at=attempted_at,
                        error=str(caught),
                    )
                    if (
                        transient_retries < self.retries
                        and getattr(caught, "retryable", False)
                    ):
                        transient_retries += 1
                        self.sleep(self.cooldown_seconds)
                        continue
                    break
            if block_outcome_recorded:
                continue
            if result is None:
                scans.append(
                    SourceScan(
                        config,
                        HealthState.FAILED,
                        error=str(error),
                        access_state=access_state,
                    )
                )
                continue
            accepted = [item for item in result.listings if matches(item, config)]
            access_state = self.repository.record_search_success(
                config,
                status=result.status_code,
                transport=result.transport,
                observed_at=self.clock(),
                health=result.health.value,
                result_count=len(accepted),
            )
            try:
                outcomes = self.repository.upsert_many(accepted)
            except Exception as caught:
                LOGGER.exception("%s persistence failed", config.name)
                scans.append(
                    SourceScan(
                        config,
                        HealthState.FAILED,
                        error=f"persistence failed: {caught}",
                        result=result,
                        access_state=access_state,
                    )
                )
                continue
            try:
                self.repository.record_scan_metadata(
                    config,
                    accepted,
                    outcomes,
                    [0.0] * len(accepted),
                )
                dynamic_scores = self.repository.update_dynamic_scores(config, accepted)
            except Exception:
                LOGGER.exception("%s scan metadata could not be persisted", config.name)
                dynamic_scores = [0.0] * len(accepted)
            events = [
                ScanEvent(item, outcome, score)
                for item, outcome, score in zip(
                    accepted, outcomes, dynamic_scores, strict=True
                )
            ]
            scans.append(
                SourceScan(
                    config,
                    result.health,
                    events,
                    result.error,
                    result,
                    access_state,
                )
            )
        return scans

    def _record_attempt(
        self,
        search: SearchConfig,
        attempted_at: datetime,
        *,
        result: SearchResult | None = None,
        error: Exception | None = None,
    ) -> None:
        if search.source != "avito":
            return
        subject = result if result is not None else error
        try:
            self.repository.record_source_attempt(
                search,
                attempted_at=attempted_at,
                status=getattr(subject, "status_code", None),
                transport=getattr(subject, "transport", None),
                impersonation=getattr(subject, "impersonation", None),
                session_mode=getattr(subject, "session_mode", None),
                listing_count=len(result.listings) if result is not None else None,
                response_cookie_names=getattr(subject, "response_cookie_names", ()),
                block_classification=getattr(
                    subject,
                    "block_classification",
                    "none",
                ),
            )
        except Exception:
            LOGGER.exception("%s attempt diagnostics could not be persisted", search.name)


def _is_explicit_block(error: Exception) -> bool:
    return getattr(error, "status_code", None) in {403, 429}
