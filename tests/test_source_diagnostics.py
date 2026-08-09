from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from src.debug_source import build_parser
from src.source_diagnostics import (
    DiagnosticConfig,
    run_diagnostic,
    sanitize_html,
    sanitize_url,
)
from src.sources.avito_transport import RetrievedPage
from src.sources.http import HttpPage

ROOT = Path(__file__).resolve().parents[1]


class FakeAvitoTransport:
    def __init__(self, page: RetrievedPage) -> None:
        self.page = page
        self.fetch_count = 0
        self.close_count = 0

    def fetch(self, url: str) -> RetrievedPage:
        self.fetch_count += 1
        return self.page

    def close(self) -> None:
        self.close_count += 1


class FakeFarPostTransport:
    def __init__(self, page: HttpPage) -> None:
        self.page = page
        self.fetch_count = 0
        self.accept_error_status: bool | None = None

    def fetch(self, url: str, *, accept_error_status: bool = False) -> HttpPage:
        self.fetch_count += 1
        self.accept_error_status = accept_error_status
        return self.page

    def close(self) -> None:
        return None


class FailingTransport:
    def __init__(self) -> None:
        self.fetch_count = 0

    def fetch(self, url: str) -> RetrievedPage:
        self.fetch_count += 1
        raise RuntimeError("cannot reach http://user:password@proxy.invalid")

    def close(self) -> None:
        return None


class DiagnosticConfigurationTests(unittest.TestCase):
    def test_direct_is_default_and_proxy_is_explicit(self) -> None:
        parser = build_parser()
        direct = parser.parse_args(["avito", "--url", "https://www.avito.ru/a"])
        proxied = parser.parse_args(
            ["avito", "--url", "https://www.avito.ru/a", "--proxy", "socks5://127.0.0.1:9050"]
        )
        self.assertFalse(direct.direct)
        self.assertIsNone(direct.proxy)
        self.assertEqual(proxied.proxy, "socks5://127.0.0.1:9050")

    def test_rejects_wrong_marketplace_host_and_bad_proxy(self) -> None:
        with self.assertRaisesRegex(ValueError, "avito.ru"):
            DiagnosticConfig("avito", "https://example.com/search").validate()
        with self.assertRaisesRegex(ValueError, "proxy"):
            DiagnosticConfig(
                "farpost", "https://www.farpost.ru/search", proxy="ftp://localhost:1"
            ).validate()
        with self.assertRaisesRegex(ValueError, "SOCKS"):
            DiagnosticConfig(
                "farpost",
                "https://www.farpost.ru/search",
                proxy="socks5://localhost:9050",
            ).validate()

    def test_sanitizes_urls_and_secret_shaped_html(self) -> None:
        self.assertEqual(
            sanitize_url("https://user:pass@example.com/a?token=secret#x"),
            "https://example.com/a",
        )
        sanitized = sanitize_html(
            '<script>{"csrfToken":"secret","name":"safe"}</script>'
        )
        self.assertNotIn("secret", sanitized)
        self.assertIn("safe", sanitized)


class SourceDiagnosticTests(unittest.TestCase):
    def test_avito_fetches_exactly_once_and_writes_sanitized_artifacts(self) -> None:
        html = (ROOT / "output" / "avito_response.html").read_text(encoding="utf-8")
        html = html.replace("</body>", '<script>{"token":"do-not-save"}</script></body>')
        transport = FakeAvitoTransport(
            RetrievedPage(
                html,
                200,
                "https://www.avito.ru/khabarovsk?q=ram&token=secret",
                "curl_cffi",
                content_type="text/html; charset=utf-8",
                impersonation="edge",
                session_mode="persistent",
                response_cookie_names=("u", "srv_id"),
            )
        )
        with tempfile.TemporaryDirectory() as temporary:
            result = run_diagnostic(
                DiagnosticConfig(
                    "avito",
                    "https://www.avito.ru/khabarovsk?q=ram&token=request-secret",
                    output_dir=Path(temporary),
                    proxy="http://proxy-user:proxy-pass@127.0.0.1:8080",
                ),
                transport=transport,
                now=datetime(2026, 8, 8, tzinfo=timezone.utc),
            )
            metadata = json.loads(result.metadata_path.read_text(encoding="utf-8"))
            saved_html = result.html_path.read_text(encoding="utf-8")

        self.assertEqual(transport.fetch_count, 1)
        self.assertEqual(result.listing_count, 50)
        self.assertEqual(result.route, "proxy")
        self.assertEqual(result.impersonation, "edge")
        self.assertEqual(result.session_mode, "persistent")
        self.assertEqual(result.response_cookie_names, ("u", "srv_id"))
        self.assertEqual(metadata["proxy_scheme"], "http")
        serialized = json.dumps(metadata)
        self.assertNotIn("proxy-user", serialized)
        self.assertNotIn("proxy-pass", serialized)
        self.assertNotIn("request-secret", serialized)
        self.assertNotIn("do-not-save", saved_html)

    def test_farpost_records_a_2xx_non_listing_page_without_raising(self) -> None:
        transport = FakeFarPostTransport(
            HttpPage(
                "<html><head><title>Information</title></head><body>Welcome</body></html>",
                200,
                "https://www.farpost.ru/search/?query=gpu",
                "text/html",
            )
        )
        with tempfile.TemporaryDirectory() as temporary:
            result = run_diagnostic(
                DiagnosticConfig(
                    "farpost",
                    "https://www.farpost.ru/search/?query=gpu",
                    output_dir=Path(temporary),
                ),
                transport=transport,
            )

        self.assertEqual(transport.fetch_count, 1)
        self.assertTrue(transport.accept_error_status)
        self.assertEqual(result.http_status, 200)
        self.assertEqual(result.page_title, "Information")
        self.assertIsNone(result.listing_count)
        self.assertIn("listing cards", result.parse_error or "")
        self.assertEqual(
            result.extraction_candidates,
            {
                "bulletin_links": 0,
                "bull_item_links": 0,
                "listing_like_html_links": 0,
                "json_ld_scripts": 0,
            },
        )

    def test_transport_failure_is_single_attempt_and_still_writes_metadata(self) -> None:
        transport = FailingTransport()
        with tempfile.TemporaryDirectory() as temporary:
            result = run_diagnostic(
                DiagnosticConfig(
                    "avito",
                    "https://www.avito.ru/search?q=secret",
                    output_dir=Path(temporary),
                ),
                transport=transport,
            )
            metadata = result.metadata_path.read_text(encoding="utf-8")
            html = result.html_path.read_text(encoding="utf-8")

        self.assertEqual(transport.fetch_count, 1)
        self.assertEqual(result.challenge_classification, "transport_error")
        self.assertIn("[REDACTED]", result.retrieval_error or "")
        self.assertNotIn("password", metadata)
        self.assertEqual(html, "")


if __name__ == "__main__":
    unittest.main()
