from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Protocol
from urllib.parse import parse_qs, urlsplit

from bs4 import BeautifulSoup
from src.retail import RetailPriceObservation
from src.retail_browser_models import BrowserCaptureResult, BrowserPageSnapshot
from src.retail_providers.common import exact_match
from src.retail_providers.dns import (
    dns_product_path_id, is_dns_challenge, parse_dns, validate_dns_url,
)
from src.retail_providers.ozon import parse_ozon, product_id_from_url, validate_ozon_url
from src.retail_providers.wildberries import extract_product_id, parse_wildberries
from src.sources.http import HttpPage


class RetailBrowserAdapter(Protocol):
    name: str

    def validate_url(self, url: str) -> tuple[str, str]: ...
    def owns_url(self, url: str) -> bool: ...
    def relevant_response(self, url: str, content_type: str) -> bool: ...
    def capture(self, snapshot: BrowserPageSnapshot, comparable_key: str,
                mapped_url: str, *, confirmed_region: str | None = None) -> BrowserCaptureResult: ...


class DnsBrowserAdapter:
    name = "dns"

    def validate_url(self, url: str) -> tuple[str, str]:
        valid = validate_dns_url(url)
        return valid, dns_product_path_id(valid) or ""

    def owns_url(self, url: str) -> bool:
        return _host(url, "dns-shop.ru")

    def relevant_response(self, url: str, content_type: str) -> bool:
        path = urlsplit(url).path.casefold()
        return self.owns_url(url) and _json_type(content_type) and any(
            marker in path for marker in ("product", "catalog", "price", "stock")
        )

    def capture(self, snapshot: BrowserPageSnapshot, comparable_key: str,
                mapped_url: str, *, confirmed_region: str | None = None) -> BrowserCaptureResult:
        _, expected = self.validate_url(mapped_url)
        if not self.owns_url(snapshot.url) or dns_product_path_id(snapshot.url) != expected:
            return _failed(self.name, "identity_mismatch", "Current DNS page does not match the saved mapping",
                           confirmed_region)
        if is_dns_challenge(HttpPage(snapshot.html, 200, snapshot.url, "text/html")):
            return _failed(self.name, "challenge", "DNS challenge/interstitial is still visible",
                           confirmed_region)
        candidates = parse_dns(snapshot.html, snapshot.url)
        for payload in snapshot.network_payloads:
            candidates.extend(parse_dns(
                '<script type="application/ld+json">'
                + json.dumps(payload.payload, ensure_ascii=False)
                + "</script>", snapshot.url))
        candidates = [row for row in candidates if not dns_product_path_id(str(row.get("url") or ""))
                      or dns_product_path_id(str(row.get("url") or "")) == expected]
        return _normalize(self.name, candidates, snapshot, comparable_key, expected,
                          confirmed_region, enforce_candidate_id=False)


class OzonBrowserAdapter:
    name = "ozon"

    def validate_url(self, url: str) -> tuple[str, str]:
        valid = validate_ozon_url(url)
        return valid, product_id_from_url(valid) or ""

    def owns_url(self, url: str) -> bool:
        return _host(url, "ozon.ru")

    def relevant_response(self, url: str, content_type: str) -> bool:
        path = urlsplit(url).path.casefold()
        return self.owns_url(url) and _json_type(content_type) and any(
            marker in path for marker in ("product", "entrypoint", "composer", "widget")
        )

    def capture(self, snapshot: BrowserPageSnapshot, comparable_key: str,
                mapped_url: str, *, confirmed_region: str | None = None) -> BrowserCaptureResult:
        _, expected = self.validate_url(mapped_url)
        if not self.owns_url(snapshot.url) or product_id_from_url(snapshot.url) != expected:
            return _failed(self.name, "identity_mismatch", "Current Ozon page does not match the saved mapping",
                           confirmed_region)
        if _challenge(snapshot.html, snapshot.title):
            return _failed(self.name, "challenge", "Ozon challenge/interstitial is still visible",
                           confirmed_region)
        candidates = parse_ozon(snapshot.html)
        for payload in snapshot.network_payloads:
            candidates.extend(parse_ozon(_json_script(payload.payload)))
        candidates = [row for row in candidates
                      if str(row.get("product_id") or "") == expected]
        return _normalize(self.name, candidates, snapshot, comparable_key, expected,
                          confirmed_region)


class WildberriesBrowserAdapter:
    name = "wildberries"

    def validate_url(self, url: str) -> tuple[str, str]:
        parts = urlsplit(url)
        product_id = extract_product_id(url)
        expected_path = re.fullmatch(r"/catalog/(\d+)/detail\.aspx/?", parts.path, re.I)
        if (parts.scheme != "https" or parts.hostname not in {"wildberries.ru", "www.wildberries.ru"}
                or not product_id or not expected_path):
            raise ValueError("mapped Wildberries URL must be a canonical HTTPS catalog product URL")
        return url, product_id

    def owns_url(self, url: str) -> bool:
        return _host(url, "wildberries.ru")

    def relevant_response(self, url: str, content_type: str) -> bool:
        host = (urlsplit(url).hostname or "").casefold()
        path = urlsplit(url).path.casefold()
        owned = any(host == domain or host.endswith(f".{domain}")
                    for domain in ("wildberries.ru", "wb.ru"))
        return owned and _json_type(content_type) and any(
            marker in path for marker in ("card", "detail", "product")
        )

    def capture(self, snapshot: BrowserPageSnapshot, comparable_key: str,
                mapped_url: str, *, confirmed_region: str | None = None) -> BrowserCaptureResult:
        _, expected = self.validate_url(mapped_url)
        if not self.owns_url(snapshot.url) or extract_product_id(snapshot.url) != expected:
            return _failed(self.name, "identity_mismatch", "Current Wildberries page does not match the saved mapping",
                           confirmed_region)
        if _challenge(snapshot.html, snapshot.title):
            return _failed(self.name, "challenge", "Wildberries challenge/interstitial is still visible",
                           confirmed_region)
        candidates: list[dict[str, object]] = []
        destination: str | None = None
        for payload in _embedded_json(snapshot.html):
            try:
                candidates.extend(row for row in parse_wildberries(
                    json.dumps(payload, ensure_ascii=False))
                    if str(row.get("product_id")) == expected)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
        for response in snapshot.network_payloads:
            try:
                rows = parse_wildberries(json.dumps(response.payload, ensure_ascii=False))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            candidates.extend(row for row in rows if str(row.get("product_id")) == expected)
            destination = destination or _known_query_value(response.url, "dest")
        region = confirmed_region
        context = _browser_region(region, f"wb_dest={destination}" if destination else None)
        return _normalize(self.name, candidates, snapshot, comparable_key, expected,
                          region, region_context=context)


ADAPTERS: dict[str, RetailBrowserAdapter] = {
    "dns": DnsBrowserAdapter(),
    "ozon": OzonBrowserAdapter(),
    "wildberries": WildberriesBrowserAdapter(),
}


def adapter_for_url(url: str) -> RetailBrowserAdapter | None:
    return next((adapter for adapter in ADAPTERS.values() if adapter.owns_url(url)), None)


def _normalize(retailer: str, candidates: list[dict[str, object]],
               snapshot: BrowserPageSnapshot, comparable_key: str, expected_id: str,
               confirmed_region: str | None,
               *, region_context: str | None = None,
               enforce_candidate_id: bool = True) -> BrowserCaptureResult:
    now = datetime.now(timezone.utc)
    context = region_context or _browser_region(confirmed_region)
    observations: list[RetailPriceObservation] = []
    for candidate in candidates:
        title = str(candidate.get("title") or "")
        matched, model = exact_match(comparable_key, title)
        candidate_id = str(candidate.get("product_id") or "")
        if not matched or enforce_candidate_id and candidate_id and candidate_id != expected_id:
            continue
        availability = str(candidate.get("availability") or "unknown")
        confidence = "exact" if availability != "unknown" else "insufficient"
        observations.append(RetailPriceObservation(
            comparable_key, retailer, int(candidate["price"]), now,
            url=str(candidate.get("url") or snapshot.url), product_title=title,
            normalized_model=model, original_price=_integer(candidate.get("original_price")),
            seller=_text(candidate.get("seller")), marketplace=retailer,
            availability=availability, region=context, match_confidence=confidence,
            delivery_price=_integer(candidate.get("delivery_price")),
            offer_id=_text(candidate.get("offer_id")), product_id=candidate_id or expected_id,
            seller_kind=_text(candidate.get("seller_kind")),
            conditional_price=_integer(candidate.get("conditional_price")),
            retrieval_method="browser-assisted",
        ))
    if not observations:
        return BrowserCaptureResult(retailer, "no_reliable_match", (),
                                    "No reliable product match or price was found", context,
                                    len(candidates))
    return BrowserCaptureResult(retailer, "captured", tuple(observations), None,
                                context, len(candidates))


def _failed(retailer: str, status: str, error: str,
            region: str | None) -> BrowserCaptureResult:
    return BrowserCaptureResult(retailer, status, (), error, _browser_region(region), 0)


def _browser_region(region: str | None, detail: str | None = None) -> str:
    if region and region.strip():
        base = f"{region.strip()}; source=browser-confirmed"
    else:
        base = "default-unresolved; source=browser"
    return f"{base}; {detail}" if detail else base


def _host(url: str, domain: str) -> bool:
    hostname = (urlsplit(url).hostname or "").casefold()
    return hostname == domain or hostname.endswith(f".{domain}")


def _json_type(content_type: str) -> bool:
    media = content_type.partition(";")[0].strip().casefold()
    return media == "application/json" or media.endswith("+json")


def _challenge(html: str, title: str) -> bool:
    value = f"{title}\n{html}".casefold()
    return any(marker in value for marker in (
        "captcha", "challenge", "access denied", "доступ ограничен",
        "проверяем, что вы не робот", "слишком много запросов",
    ))


def _json_script(payload: object) -> str:
    return '<script type="application/json">' + json.dumps(payload, ensure_ascii=False) + "</script>"


def _embedded_json(html: str) -> list[object]:
    output: list[object] = []
    for script in BeautifulSoup(html, "html.parser").select("script"):
        text = script.string or script.get_text()
        if not text or not text.lstrip().startswith(("{", "[")):
            continue
        try:
            output.append(json.loads(text))
        except ValueError:
            continue
    return output


def _known_query_value(url: str, name: str) -> str | None:
    values = parse_qs(urlsplit(url).query).get(name)
    return values[0] if values else None


def _integer(value: object) -> int | None:
    return int(value) if value not in (None, "") else None


def _text(value: object) -> str | None:
    return str(value) if value not in (None, "") else None
