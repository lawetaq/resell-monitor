from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from src.models import Listing


SOURCE_HOSTS = {
    "avito": ("avito.ru",),
    "farpost": ("farpost.ru",),
    "youla": ("youla.ru",),
}
NAVIGATION_TITLES = {
    "цена", "сортировка", "москва", "хабаровск", "женский гардероб",
    "комплектующие и запчасти", "компьютеры", "категории", "фильтры",
}
NAVIGATION_PATHS = ("/categories", "/catalog", "/filter", "/sort", "/city")


@dataclass(slots=True, frozen=True)
class CandidateQuality:
    valid: bool
    reasons: tuple[str, ...] = ()


@dataclass(slots=True, frozen=True)
class SourceQualityMetrics:
    raw_items: int
    valid_listings: int
    rejected_items: int
    priced_listings: int

    @property
    def rejection_rate(self) -> float:
        return round(self.rejected_items / self.raw_items * 100, 1) if self.raw_items else 0.0


def validate_candidate(listing: Listing) -> CandidateQuality:
    reasons: list[str] = []
    source = listing.source.casefold().strip()
    title = re.sub(r"\s+", " ", listing.title).strip() if isinstance(listing.title, str) else ""
    parts = urlsplit(listing.url)
    host = (parts.hostname or "").casefold()
    if not source or not re.fullmatch(r"[a-z][a-z0-9_-]*", source):
        reasons.append("invalid_source")
    if len(title) < 3 or title.casefold() in NAVIGATION_TITLES:
        reasons.append("non_listing_title")
    if not listing.external_id.strip():
        reasons.append("missing_external_id")
    if parts.scheme not in {"http", "https"} or not host:
        reasons.append("invalid_url")
    elif source in SOURCE_HOSTS and not any(
            host == domain or host.endswith(f".{domain}") for domain in SOURCE_HOSTS[source]):
        reasons.append("wrong_source_host")
    path = parts.path.casefold().rstrip("/")
    if not path or any(path == marker or path.endswith(marker) for marker in NAVIGATION_PATHS):
        reasons.append("navigation_url")
    if listing.price is None or listing.price <= 0:
        reasons.append("missing_price")
    return CandidateQuality(not reasons, tuple(reasons))


def partition_candidates(listings: list[Listing]) -> tuple[list[Listing], SourceQualityMetrics]:
    valid = [listing for listing in listings if validate_candidate(listing).valid]
    return valid, SourceQualityMetrics(
        raw_items=len(listings), valid_listings=len(valid),
        rejected_items=len(listings) - len(valid),
        priced_listings=sum(1 for listing in valid if listing.price is not None),
    )
