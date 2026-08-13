from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Mapping


class ListingStatus(StrEnum):
    NEW = "new"
    REVIEWED = "reviewed"
    IGNORED = "ignored"
    INTERESTING = "interesting"
    BOUGHT = "bought"


class ListingAvailability(StrEnum):
    ACTIVE = "active"
    STALE = "stale"
    DISAPPEARED = "disappeared"
    ARCHIVED = "archived"
    UNKNOWN = "unknown"


class LocationMode(StrEnum):
    DEFAULT = "default"
    SPECIFIC = "specific"
    ALL = "all"


@dataclass(slots=True, frozen=True)
class LocationProfile:
    """Application location with adapter-owned source representations."""

    id: str
    display_name: str
    country: str | None = None
    source_tokens: Mapping[str, str] = None  # type: ignore[assignment]
    aliases: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.id.strip() or not self.display_name.strip():
            raise ValueError("location id and display name must not be empty")
        object.__setattr__(self, "source_tokens", dict(self.source_tokens or {}))
        object.__setattr__(self, "aliases", tuple(self.aliases))


@dataclass(slots=True)
class Listing:
    """Marketplace-independent representation of a listing."""

    source: str
    external_id: str
    title: str
    price: int | None
    price_display: str
    location: str | None
    url: str
    description: str | None = None
    published_at: datetime | None = None
    availability: ListingAvailability = ListingAvailability.UNKNOWN


@dataclass(slots=True, frozen=True)
class SearchConfig:
    name: str
    source: str
    url: str
    enabled: bool = True
    interval_seconds: int = 900
    min_price: int | None = None
    max_price: int | None = None
    include_terms: tuple[str, ...] = ()
    exclude_terms: tuple[str, ...] = ()
    brands: tuple[str, ...] = ()
    target_price: int | None = None
    jitter_seconds: int = 0
    block_retry_delay_seconds: float = 60.0
    block_cooldown_seconds: int = 900
    max_block_retries: int = 1
    network_route: str = "direct"
    proxy_url: str | None = None
    avito_impersonation: str = "chrome"
    avito_session_mode: str = "persistent"
    preset_id: str | None = None
    location_mode: LocationMode = LocationMode.DEFAULT
    specific_location: LocationProfile | None = None
