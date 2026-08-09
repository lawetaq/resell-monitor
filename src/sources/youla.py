from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from src.models import Listing, ListingAvailability
from src.sources.base import HealthState, SearchResult
from src.sources.http import HttpTransport, HttpTransportError

BASE_URL = "https://youla.ru"


class YoulaError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


def extract_youla_state(html: str) -> dict[str, Any]:
    marker = "window.__YOULA_STATE__ ="
    position = html.find(marker)
    if position < 0:
        raise YoulaError("Youla embedded state was not found")
    raw = html[position + len(marker):].lstrip()
    try:
        value, _ = json.JSONDecoder().raw_decode(raw)
    except json.JSONDecodeError as error:
        raise YoulaError(f"invalid Youla embedded state: {error}") from error
    if not isinstance(value, dict):
        raise YoulaError("Youla embedded state has an unexpected type")
    return value


def _product_dicts(value: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if isinstance(value, dict):
        identifier = value.get("id") or value.get("productId")
        title = value.get("name") or value.get("title")
        if identifier and isinstance(title, str) and ("price" in value or "url" in value or "slug" in value):
            found.append(value)
        for child in value.values():
            found.extend(_product_dicts(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(_product_dicts(child))
    return found


def parse_youla_html(html: str) -> list[Listing]:
    state = extract_youla_state(html)
    listings: list[Listing] = []
    seen: set[str] = set()
    for item in _product_dicts(state):
        identifier = str(item.get("id") or item.get("productId"))
        if identifier in seen:
            continue
        title = str(item.get("name") or item.get("title")).strip()
        raw_price = item.get("price")
        if isinstance(raw_price, dict):
            raw_price = raw_price.get("value") or raw_price.get("amount")
        price = raw_price if isinstance(raw_price, int) and not isinstance(raw_price, bool) else None
        href = item.get("url") or item.get("productUrl") or item.get("slug")
        if not isinstance(href, str):
            continue
        location = item.get("location") or item.get("city")
        if isinstance(location, dict):
            location = location.get("name")
        seen.add(identifier)
        status = item.get("status") or item.get("availability")
        availability = (ListingAvailability.ARCHIVED
                        if isinstance(status, str) and status.casefold() in
                        {"archived", "closed", "inactive", "unavailable", "removed"}
                        else ListingAvailability.UNKNOWN)
        listings.append(Listing("youla", identifier, title, price, f"{price:,} ₽".replace(",", " ") if price is not None else "Цена не указана", location if isinstance(location, str) else None, urljoin(BASE_URL, href), availability=availability))
    return listings


class YoulaSource:
    """HTTP adapter for Youla's embedded state; reports degraded health when its feed remains client-only."""

    def __init__(self, transport: HttpTransport | None = None) -> None:
        self.transport = transport or HttpTransport()

    def search(self, url: str, *, debug_dir: Path | None = None) -> SearchResult:
        try:
            page = self.transport.fetch(url)
            listings = parse_youla_html(page.text)
        except (HttpTransportError, YoulaError) as error:
            raise YoulaError(str(error), retryable=getattr(error, "retryable", False)) from error
        artifacts: list[str] = []
        if debug_dir:
            debug_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S.%f%z")
            path = debug_dir / f"youla_{stamp}_response.html"
            path.write_text(page.text, encoding="utf-8")
            artifacts.append(str(path))
        degraded = not listings
        return SearchResult(page.status_code, listings, "requests", "embedded-state", health=HealthState.DEGRADED if degraded else HealthState.HEALTHY, error="Youla HTML contained no server-side listing feed; browser/API fallback is still required" if degraded else None, debug_artifacts=artifacts)

    def close(self) -> None:
        self.transport.close()

    def __enter__(self) -> YoulaSource:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
