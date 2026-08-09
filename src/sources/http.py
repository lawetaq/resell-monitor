from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping
from urllib.parse import parse_qsl, urljoin, urlsplit

import requests


class HttpTransportError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass(slots=True, frozen=True)
class RedirectHop:
    status_code: int
    source_host: str
    source_path: str
    destination_host: str
    destination_path: str
    added_query_names: tuple[str, ...] = ()
    removed_query_names: tuple[str, ...] = ()
    cookie_names: tuple[str, ...] = ()


@dataclass(slots=True)
class HttpPage:
    text: str
    status_code: int
    final_url: str
    content_type: str
    response_size: int | None = None
    retry_after: str | None = None
    redirect_chain: tuple[RedirectHop, ...] = ()
    redirect_classification: str = "none"


class HttpTransport:
    def __init__(
        self,
        *,
        timeout: float = 30,
        session: requests.Session | None = None,
        proxy: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self.timeout = timeout
        self.proxy = proxy
        self.headers = dict(headers or {
            "User-Agent": "Mozilla/5.0 (compatible; ResellMonitor/1.0; conservative local monitor)"
        })
        self.session = session or requests.Session()
        if proxy is not None:
            self.session.proxies.update({"http": proxy, "https": proxy})

    def fetch(self, url: str, *, accept_error_status: bool = False) -> HttpPage:
        try:
            response = self.session.get(
                url,
                timeout=self.timeout,
                headers=self.headers,
            )
        except requests.RequestException as error:
            detail = str(error).replace(self.proxy, "[REDACTED PROXY]") if self.proxy else str(error)
            raise HttpTransportError(f"request failed: {detail}", retryable=True) from error
        if not accept_error_status and not 200 <= response.status_code < 300:
            raise HttpTransportError(
                f"HTTP {response.status_code}",
                retryable=response.status_code in {408, 425, 500, 502, 503, 504},
            )
        return HttpPage(
            response.text,
            response.status_code,
            response.url,
            response.headers.get("content-type", ""),
            len(response.content),
            response.headers.get("retry-after"),
        )

    def fetch_bounded(
        self,
        url: str,
        *,
        accept_error_status: bool = False,
        max_redirects: int = 3,
    ) -> HttpPage:
        """Follow a small redirect chain while retaining session cookies.

        Redirect diagnostics intentionally retain only host/path, query *names*,
        and cookie names. Values never leave the HTTP layer.
        """
        current = url
        visited: set[str] = set()
        hops: list[RedirectHop] = []
        for _ in range(max_redirects + 1):
            canonical = _canonical_redirect_url(current)
            if canonical in visited:
                return HttpPage("", 307, current, "", 0, None, tuple(hops),
                                "redirect_loop")
            visited.add(canonical)
            try:
                response = self.session.get(
                    current, timeout=self.timeout, headers=self.headers,
                    allow_redirects=False,
                )
            except requests.RequestException as error:
                detail = str(error).replace(self.proxy, "[REDACTED PROXY]") if self.proxy else str(error)
                raise HttpTransportError(f"request failed: {detail}", retryable=True) from error
            location = response.headers.get("location")
            if 300 <= response.status_code < 400 and location:
                destination = urljoin(current, location)
                hops.append(_redirect_hop(response, current, destination))
                if _canonical_redirect_url(destination) in visited:
                    return HttpPage(response.text, response.status_code, response.url,
                                    response.headers.get("content-type", ""),
                                    len(response.content), response.headers.get("retry-after"),
                                    tuple(hops), "redirect_loop")
                current = destination
                continue
            if not accept_error_status and not 200 <= response.status_code < 300:
                raise HttpTransportError(
                    f"HTTP {response.status_code}",
                    retryable=response.status_code in {408, 425, 500, 502, 503, 504},
                )
            return HttpPage(response.text, response.status_code, response.url,
                            response.headers.get("content-type", ""), len(response.content),
                            response.headers.get("retry-after"), tuple(hops), "none")
        return HttpPage("", 307, current, "", 0, None, tuple(hops),
                        "redirect_limit")

    def close(self) -> None:
        self.session.close()

    def __enter__(self) -> HttpTransport:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def _canonical_redirect_url(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme.casefold()}://{parts.netloc.casefold()}{parts.path}?{parts.query}"


def _redirect_hop(response: requests.Response, source: str, destination: str) -> RedirectHop:
    before = urlsplit(source)
    after = urlsplit(destination)
    before_names = {name for name, _ in parse_qsl(before.query, keep_blank_values=True)}
    after_names = {name for name, _ in parse_qsl(after.query, keep_blank_values=True)}
    cookie_names = tuple(sorted(response.cookies.keys()))
    return RedirectHop(
        response.status_code, before.netloc, before.path, after.netloc, after.path,
        tuple(sorted(after_names - before_names)), tuple(sorted(before_names - after_names)),
        cookie_names,
    )
