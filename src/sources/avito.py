from __future__ import annotations

import html as html_lib
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from src.models import Listing, ListingAvailability
from src.sources.base import SearchResult
from src.sources.avito_transport import (
    AvitoTransport,
    CurlCffiTransport,
    PlaywrightTransport,
    RequestsTransport,
    RetrievedPage,
    TransportError,
    describe_page_problem,
)
from src.sources.avito_playwright_parser import (
    PlaywrightExtractionError,
    extract_dom_listings,
    extract_network_items,
)

AVITO_BASE_URL = "https://www.avito.ru"
TransportMode = Literal["auto", "curl_cffi", "requests", "playwright"]


class AvitoError(RuntimeError):
    """Ошибка получения или разбора данных Авито."""

    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        allow_fallback: bool = False,
        status_code: int | None = None,
        transport: str | None = None,
        impersonation: str | None = None,
        session_mode: str | None = None,
        response_cookie_names: tuple[str, ...] = (),
        block_classification: str = "none",
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.allow_fallback = allow_fallback
        self.status_code = status_code
        self.transport = transport
        self.impersonation = impersonation
        self.session_mode = session_mode
        self.response_cookie_names = response_cookie_names
        self.block_classification = block_classification


class AvitoSource:
    """Avito adapter with Requests primary and optional Chromium fallback."""

    def __init__(
        self,
        *,
        transport_mode: TransportMode = "auto",
        headed: bool = False,
        diagnostic_pause_seconds: float = 15.0,
        proxy: str | None = None,
        impersonation: str = "chrome",
        session_mode: Literal["persistent", "fresh"] = "persistent",
        curl_transport: AvitoTransport | None = None,
        requests_transport: AvitoTransport | None = None,
        playwright_transport: AvitoTransport | None = None,
    ) -> None:
        self.transport_mode = transport_mode
        self.curl_transport = curl_transport or CurlCffiTransport(
            proxy=proxy,
            impersonate=impersonation,
            session_mode=session_mode,
        )
        self.requests_transport = requests_transport or RequestsTransport(proxy=proxy)
        self.playwright_transport = playwright_transport or PlaywrightTransport(
            headed=headed,
            diagnostic_pause_seconds=diagnostic_pause_seconds,
            proxy=proxy,
        )

    def search(
        self,
        url: str,
        *,
        debug_dir: Path | None = None,
    ) -> SearchResult:
        debug_stamp = make_debug_stamp() if debug_dir is not None else None
        if self.transport_mode == "curl_cffi":
            return self._search_with(
                self.curl_transport,
                url,
                debug_dir=debug_dir,
                debug_stamp=debug_stamp,
            )

        if self.transport_mode == "requests":
            return self._search_with(
                self.requests_transport,
                url,
                debug_dir=debug_dir,
                debug_stamp=debug_stamp,
            )

        if self.transport_mode == "playwright":
            return self._search_with(
                self.playwright_transport,
                url,
                debug_dir=debug_dir,
                debug_stamp=debug_stamp,
            )

        failures: list[str] = []
        transports = (
            self.curl_transport,
            self.requests_transport,
            self.playwright_transport,
        )
        for index, transport in enumerate(transports):
            try:
                return self._search_with(
                    transport,
                    url,
                    debug_dir=debug_dir,
                    debug_stamp=debug_stamp,
                    fallback_reason="; ".join(failures) or None,
                )
            except AvitoError as error:
                failures.append(str(error))
                if not error.allow_fallback:
                    raise AvitoError(
                        "Avito retrieval failed. " + "; ".join(failures),
                        retryable=error.retryable,
                        status_code=error.status_code,
                        transport=error.transport,
                        impersonation=error.impersonation,
                        session_mode=error.session_mode,
                        response_cookie_names=error.response_cookie_names,
                        block_classification=error.block_classification,
                    ) from error
                if index == len(transports) - 1:
                    raise AvitoError(
                        "Avito retrieval failed. " + "; ".join(failures),
                        retryable=error.retryable,
                        status_code=error.status_code,
                        transport=error.transport,
                        impersonation=error.impersonation,
                        session_mode=error.session_mode,
                        response_cookie_names=error.response_cookie_names,
                        block_classification=error.block_classification,
                    ) from error
        raise AssertionError("unreachable")

    def _search_with(
        self,
        transport: AvitoTransport,
        url: str,
        *,
        debug_dir: Path | None,
        debug_stamp: str | None,
        fallback_reason: str | None = None,
    ) -> SearchResult:
        try:
            page = transport.fetch(url)
        except TransportError as error:
            raise AvitoError(
                str(error),
                retryable=error.retryable,
                allow_fallback=error.allow_fallback,
                transport=_transport_name(transport),
            ) from error

        if debug_dir is not None:
            save_transport_debug(debug_dir, page, debug_stamp or make_debug_stamp())

        problem = describe_page_problem(
            page.status_code,
            page.html,
            page.title,
        )
        if problem is not None:
            status = page.status_code
            classification = (
                "http-block"
                if status in {403, 429}
                else "challenge"
                if "challenge" in problem.lower() or "captcha" in problem.lower()
                else "http-error"
            )
            raise AvitoError(
                problem,
                retryable=status in {408, 425, 500, 502, 503, 504},
                allow_fallback=status in {408, 425, 500, 502, 503, 504},
                status_code=status,
                transport=page.transport,
                impersonation=page.impersonation,
                session_mode=page.session_mode,
                response_cookie_names=page.response_cookie_names,
                block_classification=classification,
            )

        if page.transport == "playwright":
            listings, extraction = self._extract_playwright(page)
        else:
            loader_data = extract_loader_data(page.html)
            raw_items = extract_items(loader_data)
            listings = [normalize_listing(item) for item in raw_items]
            extraction = "embedded-json"

            if debug_dir is not None:
                save_loader_data(
                    debug_dir,
                    loader_data,
                    debug_stamp or make_debug_stamp(),
                )

        return SearchResult(
            status_code=page.status_code,
            listings=listings,
            transport=page.transport,
            extraction=extraction,
            fallback_reason=fallback_reason,
            impersonation=page.impersonation,
            session_mode=page.session_mode,
            response_cookie_names=page.response_cookie_names,
            block_classification="none",
        )

    def close(self) -> None:
        seen: set[int] = set()
        for transport in (
            self.curl_transport,
            self.requests_transport,
            self.playwright_transport,
        ):
            if id(transport) in seen:
                continue
            seen.add(id(transport))
            close = getattr(transport, "close", None)
            if close is not None:
                close()

    def __enter__(self) -> AvitoSource:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @staticmethod
    def _extract_playwright(page: RetrievedPage) -> tuple[list[Listing], str]:
        payloads = [response.payload for response in page.network_json or []]
        raw_items = extract_network_items(payloads)
        if raw_items is not None:
            try:
                listings = [normalize_listing(item) for item in raw_items]
            except AvitoError:
                listings = []
            if listings:
                return listings, "network-json"

        try:
            return extract_dom_listings(page.html), "rendered-dom"
        except PlaywrightExtractionError as error:
            raise AvitoError(str(error)) from error


def extract_loader_data(html_text: str) -> dict[str, Any]:
    """
    Извлекает JSON с данными поисковой выдачи,
    встроенный Авито в HTML первой страницы.
    """

    soup = BeautifulSoup(html_text, "html.parser")

    scripts = soup.select(
        'script[type="mime/invalid"][data-mfe-state="true"]'
    )

    for script in scripts:
        raw_text = script.string or script.get_text()

        if not raw_text or "sandbox" in raw_text:
            continue

        try:
            decoded = html_lib.unescape(raw_text)
            payload = json.loads(decoded)
        except (json.JSONDecodeError, TypeError):
            continue

        if not isinstance(payload, dict):
            continue

        loader = payload.get("loaderData")
        if not isinstance(loader, dict):
            continue

        loader_data = loader.get("data")

        if isinstance(loader_data, dict) and loader_data:
            return loader_data

    raise AvitoError(
        "Встроенный JSON не найден. "
        "Возможно, изменилась структура страницы или показана заглушка."
    )


def extract_items(loader_data: dict[str, Any]) -> list[dict[str, Any]]:
    """Возвращает список объявлений из извлечённых данных."""

    catalog = loader_data.get("catalog", {})
    if not isinstance(catalog, dict):
        raise AvitoError("Поле catalog имеет неожиданный формат.")

    items = catalog.get("items", [])

    if not isinstance(items, list):
        raise AvitoError("Поле catalog.items имеет неожиданный формат.")

    if not all(isinstance(item, dict) for item in items):
        raise AvitoError("Поле catalog.items содержит элемент неожиданного формата.")

    return items


def normalize_listing(item: dict[str, Any]) -> Listing:
    """Convert one raw Avito catalog item to the shared model."""

    raw_id = item.get("id")
    title = item.get("title")
    url_path = item.get("urlPath")

    if raw_id is None:
        raise AvitoError("У объявления отсутствует id.")
    if not isinstance(title, str) or not title.strip():
        raise AvitoError(f"У объявления {raw_id} отсутствует название.")
    if not isinstance(url_path, str) or not url_path:
        raise AvitoError(f"У объявления {raw_id} отсутствует ссылка.")

    price: int | None = None
    price_display = "Цена не указана"
    price_detailed = item.get("priceDetailed")
    if isinstance(price_detailed, dict):
        raw_price = price_detailed.get("value")
        if isinstance(raw_price, int) and not isinstance(raw_price, bool):
            price = raw_price

        raw_display = (
            price_detailed.get("fullString")
            or price_detailed.get("string")
        )
        if isinstance(raw_display, str) and raw_display.strip():
            price_display = raw_display.strip()
        elif price is not None:
            price_display = str(price)

    location = _extract_location(item)

    return Listing(
        source="avito",
        external_id=str(raw_id),
        title=title.strip(),
        price=price,
        price_display=price_display,
        location=location,
        url=urljoin(AVITO_BASE_URL, url_path),
        availability=_explicit_avito_availability(item),
    )


def _explicit_avito_availability(item: dict[str, Any]) -> ListingAvailability:
    """Use only explicit status fields already present in search payloads."""
    values = [item.get("status"), item.get("listingStatus"), item.get("availability")]
    archived = {"archived", "archive", "closed", "inactive", "unavailable", "removed"}
    if any(isinstance(value, str) and value.casefold() in archived for value in values):
        return ListingAvailability.ARCHIVED
    if item.get("isArchived") is True or item.get("isActive") is False:
        return ListingAvailability.ARCHIVED
    return ListingAvailability.UNKNOWN


def _extract_location(item: dict[str, Any]) -> str | None:
    location = item.get("location")
    if isinstance(location, dict):
        name = location.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()

    address = item.get("addressDetailed")
    if isinstance(address, dict):
        name = address.get("locationName")
        if isinstance(name, str) and name.strip():
            return name.strip()

    geo = item.get("geo")
    if isinstance(geo, dict):
        formatted = geo.get("formattedAddress")
        if isinstance(formatted, str) and formatted.strip():
            return formatted.strip()

    return None


def save_loader_data(
    output_dir: Path,
    loader_data: dict[str, Any],
    stamp: str,
) -> None:
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / f"avito_{stamp}_embedded_data.json").write_text(
            json.dumps(loader_data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError as error:
        raise AvitoError(f"Не удалось сохранить отладочные данные: {error}") from error


def save_transport_debug(
    output_dir: Path,
    page: RetrievedPage,
    stamp: str,
) -> None:
    """Preserve one transport attempt without overwriting earlier runs."""

    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        prefix = f"avito_{stamp}_{page.transport}"
        (output_dir / f"{prefix}_response.html").write_text(
            page.html,
            encoding="utf-8",
        )

        if page.network_json is not None:
            manifest = [
                {
                    "url": response.url,
                    "status_code": response.status_code,
                }
                for response in page.network_json
            ]
            payloads = [
                {
                    "url": response.url,
                    "status_code": response.status_code,
                    "payload": response.payload,
                }
                for response in page.network_json
            ]
            (output_dir / f"{prefix}_network_manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            (output_dir / f"{prefix}_network.json").write_text(
                json.dumps(payloads, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
    except OSError as error:
        raise AvitoError(f"Не удалось сохранить отладочные данные: {error}") from error


def make_debug_stamp() -> str:
    return datetime.now().astimezone().strftime("%Y%m%dT%H%M%S.%f%z")


def _transport_name(transport: AvitoTransport) -> str:
    name = type(transport).__name__.removesuffix("Transport")
    return name.replace("Cffi", "_cffi").lower()
