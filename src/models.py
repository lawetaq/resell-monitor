from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
import ipaddress
from typing import Mapping
from urllib.parse import urlsplit


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
    primary_image_url: str | None = None

    def __post_init__(self) -> None:
        self.primary_image_url = normalize_external_image_url(self.primary_image_url)


def normalize_external_image_url(value: object) -> str | None:
    """Return a browser-loadable public HTTP(S) image URL, or ``None``.

    This validation is deliberately network-free. It rejects local/literal private
    targets but does not resolve DNS, and no backend component fetches the URL.
    """

    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not candidate or len(candidate) > 4096 or any(
        ord(character) < 32 for character in candidate
    ):
        return None
    try:
        parsed = urlsplit(candidate)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return None
    if parsed.scheme.casefold() not in {"http", "https"} or not hostname:
        return None
    if parsed.username is not None or parsed.password is not None:
        return None
    normalized_host = hostname.rstrip(".").casefold()
    if normalized_host == "localhost" or normalized_host.endswith(".localhost"):
        return None
    try:
        address = ipaddress.ip_address(normalized_host)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        return None
    if port is not None and port not in {80, 443}:
        return None
    return candidate


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
