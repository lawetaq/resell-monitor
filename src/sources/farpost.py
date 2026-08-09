from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

from src.models import Listing
from src.sources.base import SearchResult
from src.sources.http import HttpTransport, HttpTransportError

BASE_URL = "https://www.farpost.ru"


class FarPostError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


def parse_farpost_html(html: str) -> list[Listing]:
    soup = BeautifulSoup(html, "html.parser")
    listings: list[Listing] = []
    seen: set[str] = set()
    for link in soup.select("a.bulletinLink[href$='.html'], a.bull-item__self-link[href$='.html']"):
        if not isinstance(link, Tag):
            continue
        href = link.get("href")
        title = link.get_text(" ", strip=True)
        if not isinstance(href, str) or not title:
            continue
        match = re.search(r"-(\d+)\.html(?:$|\?)", href)
        if not match or match.group(1) in seen:
            continue
        seen.add(match.group(1))
        card = link.find_parent(
            lambda tag: isinstance(tag, Tag)
            and "bull-item" in (tag.get("class") or [])
        ) or link.parent
        text = card.get_text(" ", strip=True) if isinstance(card, Tag) else title
        price_match = re.search(r"(\d[\d\s\u00a0]*)\s*[₽р]", text, re.I)
        price = int(re.sub(r"\D", "", price_match.group(1))) if price_match else None
        display = price_match.group(0).strip() if price_match else "Цена не указана"
        location_node = card.select_one(".bull-deliveryGeo, .bulletinCity, [class*='geo']") if isinstance(card, Tag) else None
        location = location_node.get_text(" ", strip=True) if location_node else None
        listings.append(Listing("farpost", match.group(1), title, price, display, location, urljoin(BASE_URL, href)))
    if not listings:
        raise FarPostError("FarPost listing cards were not found")
    return listings


class FarPostSource:
    def __init__(self, transport: HttpTransport | None = None) -> None:
        self.transport = transport or HttpTransport()

    def search(self, url: str, *, debug_dir: Path | None = None) -> SearchResult:
        try:
            page = self.transport.fetch(url)
            listings = parse_farpost_html(page.text)
        except (HttpTransportError, FarPostError) as error:
            raise FarPostError(str(error), retryable=getattr(error, "retryable", False)) from error
        artifacts: list[str] = []
        if debug_dir:
            debug_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S.%f%z")
            path = debug_dir / f"farpost_{stamp}_response.html"
            path.write_text(page.text, encoding="utf-8")
            artifacts.append(str(path))
        return SearchResult(page.status_code, listings, "requests", "server-rendered-html", debug_artifacts=artifacts)

    def close(self) -> None:
        self.transport.close()

    def __enter__(self) -> FarPostSource:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
