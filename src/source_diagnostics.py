from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit, urlunsplit

from bs4 import BeautifulSoup

from src.sources.avito import extract_items, extract_loader_data, normalize_listing
from src.sources.avito_playwright_parser import extract_dom_listings, extract_network_items
from src.sources.avito_transport import (
    CurlCffiTransport,
    PlaywrightTransport,
    RequestsTransport,
    RetrievedPage,
    describe_page_problem,
)
from src.sources.farpost import FarPostError, parse_farpost_html
from src.sources.http import HttpPage, HttpTransport

SourceName = Literal["avito", "farpost"]
AvitoTransportName = Literal["curl_cffi", "requests", "playwright"]
SUPPORTED_PROXY_SCHEMES = frozenset(
    {"http", "https", "socks4", "socks4a", "socks5", "socks5h"}
)


class ClosableTransport(Protocol):
    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class DiagnosticConfig:
    source: SourceName
    url: str
    output_dir: Path = Path("output/source-debug")
    avito_transport: AvitoTransportName = "curl_cffi"
    impersonation: str = "chrome"
    session_mode: Literal["persistent", "fresh"] = "persistent"
    proxy: str | None = None
    timeout_seconds: float = 30.0
    playwright_profile_dir: Path = Path("data/playwright/diagnostic-profile")

    def validate(self) -> None:
        if self.source not in {"avito", "farpost"}:
            raise ValueError("source must be avito or farpost")
        if self.avito_transport not in {"curl_cffi", "requests", "playwright"}:
            raise ValueError("unsupported Avito transport")
        if not self.impersonation.strip():
            raise ValueError("impersonation must not be empty")
        if self.session_mode not in {"persistent", "fresh"}:
            raise ValueError("session_mode must be persistent or fresh")
        url = urlsplit(self.url)
        if url.scheme not in {"http", "https"} or not url.hostname:
            raise ValueError("URL must be an absolute HTTP or HTTPS URL")
        if self.source == "avito" and not _is_host(url.hostname, "avito.ru"):
            raise ValueError("Avito diagnostics require an avito.ru URL")
        if self.source == "farpost" and not _is_host(url.hostname, "farpost.ru"):
            raise ValueError("FarPost diagnostics require a farpost.ru URL")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout must be greater than zero")
        if self.proxy is not None:
            proxy = urlsplit(self.proxy)
            if proxy.scheme.lower() not in SUPPORTED_PROXY_SCHEMES or not proxy.hostname:
                schemes = ", ".join(sorted(SUPPORTED_PROXY_SCHEMES))
                raise ValueError(f"proxy must use one of: {schemes}")
            uses_requests = self.source == "farpost" or self.avito_transport == "requests"
            if uses_requests and proxy.scheme.lower().startswith("socks"):
                raise ValueError(
                    "SOCKS proxy URLs are not supported by the Requests transport in "
                    "this project; use an HTTP(S) proxy, or Avito curl_cffi/Playwright"
                )


@dataclass(frozen=True, slots=True)
class DiagnosticResult:
    source: str
    transport: str
    route: str
    impersonation: str | None
    session_mode: str | None
    response_cookie_names: tuple[str, ...]
    http_status: int | None
    final_url: str
    page_title: str | None
    content_type: str | None
    extraction_candidates: dict[str, int]
    challenge_classification: str
    listing_count: int | None
    parse_error: str | None
    retrieval_error: str | None
    html_path: Path
    metadata_path: Path


def run_diagnostic(
    config: DiagnosticConfig,
    *,
    transport: ClosableTransport | None = None,
    now: datetime | None = None,
) -> DiagnosticResult:
    """Fetch exactly once, inspect the response, and persist sanitized artifacts."""

    config.validate()
    selected_transport = transport or _make_transport(config)
    owns_transport = transport is None
    timestamp = now or datetime.now().astimezone()
    stamp = timestamp.strftime("%Y%m%dT%H%M%S.%f%z")
    config.output_dir.mkdir(parents=True, exist_ok=True)
    html_path = config.output_dir / f"{config.source}_{stamp}_response.html"
    metadata_path = config.output_dir / f"{config.source}_{stamp}_metadata.json"

    page: RetrievedPage | HttpPage | None = None
    retrieval_error: str | None = None
    try:
        page = _fetch_once(config, selected_transport)
    except Exception as error:
        retrieval_error = _sanitize_error(str(error), config)
    finally:
        if owns_transport:
            selected_transport.close()

    if page is None:
        html = ""
        status = None
        final_url = config.url
        transport_name = (
            "requests" if config.source == "farpost" else config.avito_transport
        )
        content_type = None
        title = None
        classification = "transport_error"
        candidates: dict[str, int] = {}
        listing_count = None
        parse_error = None
    else:
        html, status, final_url, transport_name, content_type, supplied_title = _page_fields(page)
        soup = BeautifulSoup(html, "html.parser")
        title = supplied_title or (soup.title.get_text(" ", strip=True) if soup.title else None)
        problem = describe_page_problem(status, html, title)
        classification = problem or "none"
        candidates, listing_count, parse_error = _inspect_content(
            config.source, page, html, problem
        )

    # Response markup is retained verbatim except for credential-shaped values.
    # Request/response headers and the cookie jar are never persisted.
    html_path.write_text(sanitize_html(html), encoding="utf-8")
    metadata = {
        "timestamp": timestamp.isoformat(),
        "source": config.source,
        "transport": transport_name,
        "route": "proxy" if config.proxy else "direct",
        "impersonation": getattr(page, "impersonation", None)
        or (
            config.impersonation
            if config.source == "avito" and config.avito_transport == "curl_cffi"
            else None
        ),
        "session_mode": getattr(page, "session_mode", None)
        or (
            config.session_mode
            if config.source == "avito" and config.avito_transport == "curl_cffi"
            else None
        ),
        "response_cookie_names": list(
            getattr(page, "response_cookie_names", ())
        ),
        "proxy_scheme": urlsplit(config.proxy).scheme.lower() if config.proxy else None,
        "requested_url": sanitize_url(config.url),
        "http_status": status,
        "final_url": sanitize_url(final_url),
        "page_title": title,
        "content_type": content_type,
        "extraction_candidates": candidates,
        "challenge_classification": classification,
        "listing_count": listing_count,
        "parse_error": parse_error,
        "retrieval_error": retrieval_error,
        "html_sanitized": True,
    }
    metadata_path.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return DiagnosticResult(
        source=config.source,
        transport=transport_name,
        route=metadata["route"],
        impersonation=metadata["impersonation"],
        session_mode=metadata["session_mode"],
        response_cookie_names=tuple(metadata["response_cookie_names"]),
        http_status=status,
        final_url=metadata["final_url"],
        page_title=title,
        content_type=content_type,
        extraction_candidates=candidates,
        challenge_classification=classification,
        listing_count=listing_count,
        parse_error=parse_error,
        retrieval_error=retrieval_error,
        html_path=html_path,
        metadata_path=metadata_path,
    )


def result_as_json(result: DiagnosticResult) -> str:
    payload = asdict(result)
    payload["html_path"] = str(result.html_path)
    payload["metadata_path"] = str(result.metadata_path)
    return json.dumps(payload, ensure_ascii=False, indent=2)


def sanitize_url(url: str) -> str:
    parts = urlsplit(url)
    hostname = parts.hostname or ""
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    netloc = hostname
    if parts.port is not None:
        netloc = f"{netloc}:{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


def sanitize_html(html: str) -> str:
    """Redact credential-shaped fields while retaining diagnostic markup."""

    sensitive = r"(?:authorization|cookie|csrf(?:token)?|session(?:id)?|sessid|access_?token|refresh_?token|token)"
    quoted = re.compile(
        rf"(?i)([\"']?{sensitive}[\"']?\s*[:=]\s*)([\"'])(.*?)(\2)",
        re.DOTALL,
    )
    unquoted = re.compile(
        rf"(?i)([\"']?{sensitive}[\"']?\s*[:=]\s*)([^\s,;}}<]+)"
    )
    redacted = quoted.sub(lambda match: f'{match.group(1)}"[REDACTED]"', html)
    return unquoted.sub(lambda match: f'{match.group(1)}"[REDACTED]"', redacted)


def _make_transport(config: DiagnosticConfig) -> ClosableTransport:
    if config.source == "farpost":
        return HttpTransport(timeout=config.timeout_seconds, proxy=config.proxy)
    if config.avito_transport == "curl_cffi":
        return CurlCffiTransport(
            timeout=int(config.timeout_seconds),
            proxy=config.proxy,
            impersonate=config.impersonation,
            session_mode=config.session_mode,
        )
    if config.avito_transport == "requests":
        return RequestsTransport(
            timeout=int(config.timeout_seconds),
            proxy=config.proxy,
        )
    return PlaywrightTransport(
        profile_dir=config.playwright_profile_dir,
        timeout_ms=int(config.timeout_seconds * 1000),
        diagnostic_pause_seconds=0,
        proxy=config.proxy,
    )


def _fetch_once(config: DiagnosticConfig, transport: ClosableTransport) -> RetrievedPage | HttpPage:
    if config.source == "farpost":
        return transport.fetch(config.url, accept_error_status=True)  # type: ignore[attr-defined]
    return transport.fetch(config.url)  # type: ignore[attr-defined]


def _page_fields(
    page: RetrievedPage | HttpPage,
) -> tuple[str, int | None, str, str, str | None, str | None]:
    if isinstance(page, RetrievedPage):
        return (
            page.html,
            page.status_code,
            page.final_url,
            page.transport,
            page.content_type,
            page.title,
        )
    return page.text, page.status_code, page.final_url, "requests", page.content_type, None


def _inspect_content(
    source: SourceName,
    page: RetrievedPage | HttpPage,
    html: str,
    problem: str | None,
) -> tuple[dict[str, int], int | None, str | None]:
    soup = BeautifulSoup(html, "html.parser")
    if source == "farpost":
        candidates = {
            "bulletin_links": len(soup.select("a.bulletinLink[href]")),
            "bull_item_links": len(soup.select("a.bull-item__self-link[href]")),
            "listing_like_html_links": len(soup.select("a[href$='.html']")),
            "json_ld_scripts": len(soup.select('script[type="application/ld+json"]')),
        }
        try:
            listings = parse_farpost_html(html) if problem is None else []
            return candidates, len(listings) if problem is None else None, None
        except FarPostError as error:
            return candidates, None, str(error)

    candidates = {
        "embedded_state_scripts": len(
            soup.select('script[type="mime/invalid"][data-mfe-state="true"]')
        ),
        "dom_listing_cards": len(soup.select('[data-marker="item"]')),
        "product_schema_nodes": len(soup.select('[itemtype$="/Product"]')),
        "captured_json_responses": len(page.network_json or [])
        if isinstance(page, RetrievedPage)
        else 0,
    }
    if problem is not None:
        return candidates, None, None
    try:
        if isinstance(page, RetrievedPage) and page.transport == "playwright":
            raw_items = extract_network_items(page.network_json or [])
            if raw_items:
                return candidates, len([normalize_listing(item) for item in raw_items]), None
            return candidates, len(extract_dom_listings(html)), None
        loader = extract_loader_data(html)
        return candidates, len([normalize_listing(item) for item in extract_items(loader)]), None
    except Exception as error:
        return candidates, None, f"{type(error).__name__}: {error}"


def _is_host(hostname: str, expected: str) -> bool:
    hostname = hostname.lower().rstrip(".")
    return hostname == expected or hostname.endswith(f".{expected}")


def _sanitize_error(message: str, config: DiagnosticConfig) -> str:
    sanitized = message.replace(config.url, sanitize_url(config.url))
    if config.proxy:
        sanitized = sanitized.replace(config.proxy, "[REDACTED PROXY]")
    return re.sub(r"(?i)(https?://)[^/@\s]+@", r"\1[REDACTED]@", sanitized)
