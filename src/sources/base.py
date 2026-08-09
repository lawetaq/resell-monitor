from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from src.models import Listing


class HealthState(StrEnum):
    HEALTHY = "healthy"
    TEMPORARILY_BLOCKED = "temporarily-blocked"
    COOLDOWN = "cooldown"
    DEGRADED = "degraded"
    EXPERIMENTAL = "experimental"
    FAILED = "failed"


@dataclass(slots=True)
class SearchResult:
    """Transport-neutral result returned by every marketplace source."""

    status_code: int | None
    listings: list[Listing]
    transport: str
    extraction: str
    fallback_reason: str | None = None
    health: HealthState = HealthState.HEALTHY
    error: str | None = None
    checked_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    debug_artifacts: list[str] = field(default_factory=list)
    impersonation: str | None = None
    session_mode: str | None = None
    response_cookie_names: tuple[str, ...] = ()
    block_classification: str = "none"


class MarketplaceSource(Protocol):
    """Minimal interface used by application orchestration."""

    def search(
        self,
        url: str,
        *,
        debug_dir: Path | None = None,
    ) -> SearchResult:
        """Fetch and normalize listings from a marketplace search URL."""
        ...

    def close(self) -> None:
        """Release owned HTTP or browser resources."""
        ...
