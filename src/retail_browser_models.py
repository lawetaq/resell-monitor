from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(slots=True, frozen=True)
class BrowserNetworkPayload:
    url: str
    status_code: int
    content_type: str
    payload: Any


@dataclass(slots=True, frozen=True)
class BrowserPageSnapshot:
    retailer: str
    url: str
    title: str
    html: str
    network_payloads: tuple[BrowserNetworkPayload, ...] = ()


@dataclass(slots=True, frozen=True)
class BrowserCaptureResult:
    retailer: str
    status: str
    observations: tuple[object, ...] = ()
    error: str | None = None
    region_context: str = "default-unresolved; source=browser"
    candidates_found: int = 0
