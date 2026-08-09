from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.7",
    "Cache-Control": "no-cache",
}

LOADER_SELECTOR = 'script[type="mime/invalid"][data-mfe-state="true"]'
ITEM_SELECTOR = '[data-marker="item"]'
READY_SELECTOR = f"{LOADER_SELECTOR}, {ITEM_SELECTOR}"
CHALLENGE_MARKERS = (
    "проверяем, что вы не робот",
    "доступ временно ограничен",
    "слишком много запросов",
    "пройдите проверку",
    "доступ ограничен",
    "captcha",
)


@dataclass(slots=True)
class NetworkJsonResponse:
    """Sanitized JSON response captured from an Avito-owned host."""

    url: str
    status_code: int
    payload: Any


@dataclass(slots=True)
class RetrievedPage:
    """HTML document returned by an Avito transport."""

    html: str
    status_code: int | None
    final_url: str
    transport: str
    title: str | None = None
    network_json: list[NetworkJsonResponse] | None = None
    content_type: str | None = None
    impersonation: str | None = None
    session_mode: str | None = None
    response_cookie_names: tuple[str, ...] = ()


class TransportError(RuntimeError):
    """A transport could not retrieve an Avito page."""

    def __init__(
        self,
        message: str,
        *,
        retryable: bool = True,
        allow_fallback: bool = True,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.allow_fallback = allow_fallback


class AvitoTransport(Protocol):
    def fetch(self, url: str) -> RetrievedPage:
        """Retrieve a page without parsing marketplace data."""
        ...

    def close(self) -> None:
        """Release resources owned by the transport."""
        ...


class CurlCffiTransport:
    """Persistent browser-impersonating HTTP session with ordinary cookies."""

    def __init__(
        self,
        timeout: int = 30,
        *,
        impersonate: str = "chrome",
        proxy: str | None = None,
        session_mode: Literal["persistent", "fresh"] = "persistent",
    ) -> None:
        if session_mode not in {"persistent", "fresh"}:
            raise ValueError("session_mode must be persistent or fresh")
        self.timeout = timeout
        self.impersonate = impersonate
        self.proxy = proxy
        self.session_mode = session_mode
        self._session: Any | None = None

    def _build_session(self) -> Any:
        if self.session_mode == "persistent" and self._session is not None:
            return self._session
        try:
            from curl_cffi import requests as curl_requests
        except ImportError as error:
            raise TransportError(
                "curl_cffi is not installed",
                retryable=False,
                allow_fallback=True,
            ) from error
        session_options: dict[str, Any] = {"impersonate": self.impersonate}
        if self.proxy is not None:
            session_options["proxies"] = {
                "http": self.proxy,
                "https": self.proxy,
            }
        session = curl_requests.Session(**session_options)
        if self.session_mode == "persistent":
            self._session = session
        return session

    def fetch(self, url: str) -> RetrievedPage:
        session = self._build_session()
        try:
            response = session.get(url, timeout=self.timeout, allow_redirects=True)
        except Exception as error:
            detail = _redact_proxy(str(error), self.proxy)
            raise TransportError(f"curl_cffi error: {detail}", retryable=True) from error
        finally:
            if self.session_mode == "fresh":
                session.close()
        return RetrievedPage(
            html=response.text,
            status_code=response.status_code,
            final_url=response.url,
            transport="curl_cffi",
            content_type=response.headers.get("content-type"),
            impersonation=self.impersonate,
            session_mode=self.session_mode,
            response_cookie_names=_cookie_names(response),
        )

    def close(self) -> None:
        if self._session is not None:
            self._session.close()
            self._session = None

    def __enter__(self) -> CurlCffiTransport:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


class RequestsTransport:
    """Primary transport based on ordinary HTTP requests."""

    def __init__(self, timeout: int = 30, *, proxy: str | None = None) -> None:
        self.timeout = timeout
        self.proxy = proxy
        self.session = requests.Session()
        if proxy is not None:
            self.session.proxies.update({"http": proxy, "https": proxy})

    def fetch(self, url: str) -> RetrievedPage:
        try:
            response = self.session.get(
                url,
                headers=HEADERS,
                timeout=self.timeout,
                allow_redirects=True,
            )
        except requests.RequestException as error:
            detail = _redact_proxy(str(error), self.proxy)
            raise TransportError(f"Requests error: {detail}") from error

        return RetrievedPage(
            html=response.text,
            status_code=response.status_code,
            final_url=response.url,
            transport="requests",
            content_type=response.headers.get("content-type"),
        )

    def close(self) -> None:
        self.session.close()

    def __enter__(self) -> RequestsTransport:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


class PlaywrightTransport:
    """Chromium transport with persistent, ordinary browser state."""

    def __init__(
        self,
        *,
        profile_dir: Path = Path("data/playwright/avito-profile"),
        headed: bool = False,
        timeout_ms: int = 60_000,
        loader_wait_ms: int = 5_000,
        diagnostic_pause_seconds: float = 15.0,
        proxy: str | None = None,
    ) -> None:
        self.profile_dir = profile_dir
        self.headed = headed
        self.timeout_ms = timeout_ms
        self.loader_wait_ms = loader_wait_ms
        self.diagnostic_pause_seconds = max(0.0, diagnostic_pause_seconds)
        self.proxy = proxy

    def fetch(self, url: str) -> RetrievedPage:
        try:
            from playwright.sync_api import (
                Error as PlaywrightError,
                TimeoutError as PlaywrightTimeoutError,
                sync_playwright,
            )
        except ImportError as error:
            raise TransportError(
                "Playwright is not installed. Run: pip install -r requirements.txt"
            ) from error

        self.profile_dir.parent.mkdir(parents=True, exist_ok=True)

        try:
            with sync_playwright() as playwright:
                context_options: dict[str, Any] = {
                    "user_data_dir": self.profile_dir,
                    "headless": not self.headed,
                    "locale": "ru-RU",
                }
                if self.proxy is not None:
                    context_options["proxy"] = {"server": self.proxy}
                context = playwright.chromium.launch_persistent_context(
                    **context_options,
                )
                try:
                    page = context.pages[0] if context.pages else context.new_page()
                    network_json: list[NetworkJsonResponse] = []

                    def capture_json(response: object) -> None:
                        try:
                            response_url = response.url  # type: ignore[attr-defined]
                            if not is_avito_owned_url(response_url):
                                return

                            content_type = response.header_value(  # type: ignore[attr-defined]
                                "content-type"
                            )
                            if not is_json_content_type(content_type):
                                return

                            payload = response.json()  # type: ignore[attr-defined]
                            network_json.append(
                                NetworkJsonResponse(
                                    url=safe_response_url(response_url),
                                    status_code=response.status,  # type: ignore[attr-defined]
                                    payload=sanitize_json(payload),
                                )
                            )
                        except (PlaywrightError, TypeError, ValueError):
                            return

                    page.on("response", capture_json)
                    response = page.goto(
                        url,
                        wait_until="domcontentloaded",
                        timeout=self.timeout_ms,
                    )

                    content_found = True
                    try:
                        page.wait_for_selector(
                            READY_SELECTOR,
                            state="attached",
                            timeout=self.loader_wait_ms,
                        )
                    except PlaywrightTimeoutError:
                        content_found = False

                    html = page.content()
                    title = page.title()
                    status_code = response.status if response is not None else None
                    content_type = (
                        response.header_value("content-type")
                        if response is not None
                        else None
                    )
                    problem = describe_page_problem(status_code, html, title)

                    if not content_found and problem is None:
                        problem = "Avito listing content did not appear"

                    if self.headed:
                        if problem is not None:
                            diagnostic_state = problem
                        elif page.locator(ITEM_SELECTOR).count() > 0:
                            diagnostic_state = "rendered Avito listing cards detected"
                        else:
                            diagnostic_state = "embedded Avito loader markup detected"
                        self._pause_for_diagnostics(page, diagnostic_state)
                        html = page.content()
                        title = page.title()

                    return RetrievedPage(
                        html=html,
                        status_code=status_code,
                        final_url=page.url,
                        transport="playwright",
                        title=title,
                        network_json=network_json,
                        content_type=content_type,
                    )
                finally:
                    context.close()
        except PlaywrightError as error:
            detail = _redact_proxy(str(error), self.proxy)
            raise TransportError(
                "Playwright Chromium failed. The browser profile may be locked, "
                "or Chromium may not be installed. "
                "Run: playwright install chromium. "
                f"Details: {detail}"
            ) from error

    def _pause_for_diagnostics(self, page: object, problem: str) -> None:
        seconds = self.diagnostic_pause_seconds
        print(f"Playwright diagnostic state: {problem}")
        print(f"Playwright page URL: {getattr(page, 'url', 'unknown')}")
        if seconds <= 0:
            return

        print(
            f"Leaving headed Chromium open for {seconds:g} seconds "
            "for visual inspection."
        )
        page.wait_for_timeout(seconds * 1000)  # type: ignore[attr-defined]

    def close(self) -> None:
        # Each fetch owns and closes its context in a finally block.
        return None

    def __enter__(self) -> PlaywrightTransport:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def is_avito_owned_url(url: str) -> bool:
    """Accept only Avito web properties, never arbitrary third-party hosts."""

    hostname = (urlsplit(url).hostname or "").lower()
    return any(
        hostname == domain or hostname.endswith(f".{domain}")
        for domain in ("avito.ru", "avito.st")
    )


def is_json_content_type(content_type: str | None) -> bool:
    if not content_type:
        return False
    media_type = content_type.partition(";")[0].strip().lower()
    return media_type == "application/json" or media_type.endswith("+json")


def safe_response_url(url: str) -> str:
    """Keep endpoint identity while dropping query and fragment values."""

    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def sanitize_json(value: Any) -> Any:
    """Remove common credential/session fields from captured diagnostics."""

    sensitive_fragments = (
        "authorization",
        "cookie",
        "csrf",
        "session",
        "sessid",
        "token",
    )
    if isinstance(value, dict):
        return {
            str(key): sanitize_json(item)
            for key, item in value.items()
            if not any(fragment in str(key).lower() for fragment in sensitive_fragments)
        }
    if isinstance(value, list):
        return [sanitize_json(item) for item in value]
    return value


def _cookie_names(response: Any) -> tuple[str, ...]:
    """Extract safe cookie names without retaining or logging their values."""

    try:
        return tuple(sorted({str(name) for name in response.cookies.keys()}))
    except (AttributeError, TypeError):
        return ()


def _redact_proxy(message: str, proxy: str | None) -> str:
    return message.replace(proxy, "[REDACTED PROXY]") if proxy else message


def describe_page_problem(
    status_code: int | None,
    html: str,
    title: str | None = None,
) -> str | None:
    """Return a clear reason when a page is blocked or otherwise unavailable."""

    if status_code in {403, 429}:
        return f"HTTP {status_code}"
    if status_code is not None and not 200 <= status_code < 300:
        return f"HTTP {status_code}"

    soup = BeautifulSoup(html, "html.parser")
    visible_text = soup.get_text(" ", strip=True)
    searchable = f"{title or ''}\n{visible_text}".lower()
    for marker in CHALLENGE_MARKERS:
        if marker in searchable:
            return f"challenge page containing {marker!r}"

    captcha_element = soup.select_one(
        '[data-marker*="captcha"], iframe[src*="captcha"], form[action*="captcha"]'
    )
    if captcha_element is not None:
        return "CAPTCHA challenge page"

    return None
