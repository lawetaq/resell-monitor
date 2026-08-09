from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from src.models import ListingAvailability


@dataclass(slots=True, frozen=True)
class FreshnessPolicy:
    """Conservative current-opportunity policy shared by storage and presentation."""

    active_for: timedelta = timedelta(hours=48)
    stale_is_actionable: bool = False
    unknown_is_actionable: bool = False

    def effective_availability(
        self,
        stored: ListingAvailability,
        *,
        last_seen: datetime | None,
        now: datetime | None = None,
    ) -> ListingAvailability:
        if stored in {ListingAvailability.ARCHIVED, ListingAvailability.DISAPPEARED}:
            return stored
        if stored is ListingAvailability.UNKNOWN:
            return stored
        if last_seen is None:
            return ListingAvailability.UNKNOWN
        current = _aware(now or datetime.now(timezone.utc))
        return (
            ListingAvailability.ACTIVE
            if current - _aware(last_seen) <= self.active_for
            else ListingAvailability.STALE
        )

    def is_actionable(self, availability: ListingAvailability) -> bool:
        return (
            availability is ListingAvailability.ACTIVE
            or self.stale_is_actionable and availability is ListingAvailability.STALE
            or self.unknown_is_actionable and availability is ListingAvailability.UNKNOWN
        )


def unavailable_recommendation(availability: ListingAvailability) -> str:
    return {
        ListingAvailability.STALE: "STALE — NOT ACTIONABLE",
        ListingAvailability.DISAPPEARED: "DISAPPEARED — UNAVAILABLE",
        ListingAvailability.ARCHIVED: "ARCHIVED — UNAVAILABLE",
        ListingAvailability.UNKNOWN: "AVAILABILITY NOT CONFIRMED",
    }.get(availability, "")


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
