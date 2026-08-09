from __future__ import annotations

import re
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

from src.models import Listing

AVITO_BASE_URL = "https://www.avito.ru"


class PlaywrightExtractionError(RuntimeError):
    """Rendered Playwright data could not be converted to listings."""


def extract_network_items(payloads: list[Any]) -> list[dict[str, Any]] | None:
    """Find the strongest listing-shaped item collection without assuming a URL."""

    candidates: list[list[dict[str, Any]]] = []
    for payload in payloads:
        candidates.extend(_find_item_lists(payload))

    if not candidates:
        return None
    return max(candidates, key=len)


def _find_item_lists(value: Any) -> list[list[dict[str, Any]]]:
    found: list[list[dict[str, Any]]] = []
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "items" and isinstance(child, list):
                recognized = [
                    item
                    for item in child
                    if isinstance(item, dict) and _looks_like_listing(item)
                ]
                if recognized:
                    found.append(recognized)
            found.extend(_find_item_lists(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(_find_item_lists(child))
    return found


def _looks_like_listing(item: dict[str, Any]) -> bool:
    return (
        item.get("id") is not None
        and isinstance(item.get("title"), str)
        and isinstance(item.get("urlPath"), str)
    )


def extract_dom_listings(html_text: str) -> list[Listing]:
    """Extract normalized listings from semantic Avito card markup."""

    soup = BeautifulSoup(html_text, "html.parser")
    cards = soup.select('[data-marker="item"]')
    if not cards:
        cards = soup.select('[itemscope][itemtype$="/Product"]')
    if not cards:
        raise PlaywrightExtractionError("Rendered Avito listing cards were not found.")

    listings: list[Listing] = []
    seen_ids: set[str] = set()
    for card in cards:
        listing = _extract_dom_listing(card)
        if listing.external_id in seen_ids:
            continue
        seen_ids.add(listing.external_id)
        listings.append(listing)

    return listings


def _extract_dom_listing(card: Tag) -> Listing:
    title_link = card.select_one('a[data-marker="item-title"]')
    if title_link is None:
        title_link = card.select_one('a[itemprop="url"]')

    title_element = title_link or card.select_one('[itemprop="name"]')
    title = title_element.get_text(" ", strip=True) if title_element else ""
    href = title_link.get("href") if title_link else None
    if not isinstance(href, str) or not href:
        raise PlaywrightExtractionError("Rendered listing is missing its URL.")

    external_id = card.get("data-item-id")
    if not isinstance(external_id, str) or not external_id:
        match = re.search(r"_(\d+)(?:\?|$)", href)
        external_id = match.group(1) if match else None

    if not external_id:
        raise PlaywrightExtractionError("Rendered listing is missing its ID.")
    if not title:
        raise PlaywrightExtractionError(
            f"Rendered listing {external_id} is missing its title."
        )

    price: int | None = None
    price_meta = card.select_one('meta[itemprop="price"]')
    if price_meta is not None:
        raw_price = price_meta.get("content")
        if isinstance(raw_price, str) and raw_price.isdigit():
            price = int(raw_price)

    price_element = card.select_one('[data-marker="item-price-value"]')
    if price_element is None:
        price_element = card.select_one('[data-marker="item-price"]')
    price_display = (
        price_element.get_text(" ", strip=True)
        if price_element is not None
        else "Цена не указана"
    )
    if not price_display and price is not None:
        price_display = str(price)

    location_element = card.select_one('[data-marker="item-location"]')
    location = (
        location_element.get_text(" ", strip=True)
        if location_element is not None
        else ""
    )

    return Listing(
        source="avito",
        external_id=external_id,
        title=title,
        price=price,
        price_display=price_display or "Цена не указана",
        location=location or None,
        url=urljoin(AVITO_BASE_URL, href),
    )
