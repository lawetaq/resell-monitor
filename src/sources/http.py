from __future__ import annotations

from dataclasses import dataclass

import requests


class HttpTransportError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass(slots=True)
class HttpPage:
    text: str
    status_code: int
    final_url: str
    content_type: str


class HttpTransport:
    def __init__(
        self,
        *,
        timeout: float = 30,
        session: requests.Session | None = None,
        proxy: str | None = None,
    ) -> None:
        self.timeout = timeout
        self.proxy = proxy
        self.session = session or requests.Session()
        if proxy is not None:
            self.session.proxies.update({"http": proxy, "https": proxy})

    def fetch(self, url: str, *, accept_error_status: bool = False) -> HttpPage:
        try:
            response = self.session.get(
                url,
                timeout=self.timeout,
                headers={"User-Agent": "Mozilla/5.0 (compatible; ResellMonitor/1.0; conservative local monitor)"},
            )
        except requests.RequestException as error:
            detail = str(error).replace(self.proxy, "[REDACTED PROXY]") if self.proxy else str(error)
            raise HttpTransportError(f"request failed: {detail}", retryable=True) from error
        if not accept_error_status and not 200 <= response.status_code < 300:
            raise HttpTransportError(
                f"HTTP {response.status_code}",
                retryable=response.status_code in {408, 425, 500, 502, 503, 504},
            )
        return HttpPage(response.text, response.status_code, response.url, response.headers.get("content-type", ""))

    def close(self) -> None:
        self.session.close()

    def __enter__(self) -> HttpTransport:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
