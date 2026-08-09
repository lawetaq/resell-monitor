from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory

from src.main import run
from src.sources.avito import AvitoError, AvitoSource
from src.sources.avito_transport import (
    NetworkJsonResponse,
    PlaywrightTransport,
    RetrievedPage,
    TransportError,
    describe_page_problem,
)

FIXTURE = Path(__file__).resolve().parents[1] / "output" / "avito_response.html"


class FakeTransport:
    def __init__(
        self,
        result: RetrievedPage | None = None,
        error: Exception | None = None,
    ) -> None:
        self.result = result
        self.error = error
        self.calls: list[str] = []

    def fetch(self, url: str) -> RetrievedPage:
        self.calls.append(url)
        if self.error is not None:
            raise self.error
        if self.result is None:
            raise AssertionError("Fake transport has no result")
        return self.result


def fixture_page(transport: str = "requests") -> RetrievedPage:
    return RetrievedPage(
        html=FIXTURE.read_text(encoding="utf-8"),
        status_code=200,
        final_url="https://www.avito.ru/test",
        transport=transport,
        title="Avito search",
        network_json=[] if transport == "playwright" else None,
    )


class AvitoSourceTransportTests(unittest.TestCase):
    def test_requests_success_does_not_use_playwright(self) -> None:
        requests = FakeTransport(fixture_page())
        playwright = FakeTransport(error=AssertionError("must not be called"))
        source = AvitoSource(
            curl_transport=requests,
            requests_transport=requests,
            playwright_transport=playwright,
        )

        result = source.search("https://www.avito.ru/test")

        self.assertEqual(result.transport, "requests")
        self.assertEqual(result.extraction, "embedded-json")
        self.assertEqual(len(result.listings), 50)
        self.assertEqual(len(requests.calls), 1)
        self.assertEqual(playwright.calls, [])

    def test_http_429_stops_without_fallback(self) -> None:
        requests = FakeTransport(
            RetrievedPage("blocked", 429, "https://www.avito.ru/test", "requests")
        )
        playwright = FakeTransport(fixture_page("playwright"))
        source = AvitoSource(
            curl_transport=requests,
            requests_transport=requests,
            playwright_transport=playwright,
        )

        with self.assertRaisesRegex(AvitoError, "HTTP 429"):
            source.search("https://www.avito.ru/test")
        self.assertEqual(playwright.calls, [])

    def test_http_403_stops_without_fallback(self) -> None:
        requests = FakeTransport(
            RetrievedPage("blocked", 403, "https://www.avito.ru/test", "requests")
        )
        playwright = FakeTransport(fixture_page("playwright"))
        source = AvitoSource(
            curl_transport=requests,
            requests_transport=requests,
            playwright_transport=playwright,
        )

        with self.assertRaisesRegex(AvitoError, "HTTP 403"):
            source.search("https://www.avito.ru/test")
        self.assertEqual(playwright.calls, [])

    def test_network_error_falls_back_to_playwright(self) -> None:
        requests = FakeTransport(error=TransportError("connection failed"))
        playwright = FakeTransport(fixture_page("playwright"))
        source = AvitoSource(
            curl_transport=requests,
            requests_transport=playwright,
            playwright_transport=playwright,
        )

        result = source.search("https://www.avito.ru/test")

        self.assertEqual(result.transport, "playwright")
        self.assertEqual(result.fallback_reason, "connection failed")

    def test_challenge_page_stops_without_fallback(self) -> None:
        requests = FakeTransport(
            RetrievedPage(
                "<html>Проверяем, что вы не робот</html>",
                200,
                "https://www.avito.ru/test",
                "requests",
            )
        )
        playwright = FakeTransport(fixture_page("playwright"))
        source = AvitoSource(
            curl_transport=requests,
            requests_transport=requests,
            playwright_transport=playwright,
        )

        with self.assertRaisesRegex(AvitoError, "challenge page"):
            source.search("https://www.avito.ru/test")
        self.assertEqual(playwright.calls, [])

    def test_unparseable_html_stops_without_fallback(self) -> None:
        requests = FakeTransport(
            RetrievedPage("<html></html>", 200, "url", "requests")
        )
        playwright = FakeTransport(fixture_page("playwright"))
        source = AvitoSource(
            curl_transport=requests,
            requests_transport=requests,
            playwright_transport=playwright,
        )

        with self.assertRaisesRegex(AvitoError, "JSON не найден"):
            source.search("https://www.avito.ru/test")
        self.assertEqual(playwright.calls, [])

    def test_requests_mode_never_falls_back(self) -> None:
        requests = FakeTransport(RetrievedPage("blocked", 429, "url", "requests"))
        playwright = FakeTransport(fixture_page("playwright"))
        source = AvitoSource(
            transport_mode="requests",
            requests_transport=requests,
            playwright_transport=playwright,
        )

        with self.assertRaisesRegex(AvitoError, "HTTP 429"):
            source.search("https://www.avito.ru/test")

        self.assertEqual(playwright.calls, [])

    def test_playwright_mode_skips_requests(self) -> None:
        requests = FakeTransport(error=AssertionError("must not be called"))
        playwright = FakeTransport(fixture_page("playwright"))
        source = AvitoSource(
            transport_mode="playwright",
            requests_transport=requests,
            playwright_transport=playwright,
        )

        result = source.search("https://www.avito.ru/test")

        self.assertEqual(result.transport, "playwright")
        self.assertEqual(requests.calls, [])

    def test_both_transport_failures_are_reported(self) -> None:
        curl = FakeTransport(error=TransportError("temporary", retryable=True))
        requests = FakeTransport(error=TransportError("temporary again", retryable=True))
        playwright = FakeTransport(
            RetrievedPage(
                "<html>Доступ ограничен</html>",
                200,
                "url",
                "playwright",
            )
        )
        source = AvitoSource(
            curl_transport=curl,
            requests_transport=requests,
            playwright_transport=playwright,
        )

        with self.assertRaisesRegex(
            AvitoError,
            "Avito retrieval failed.*temporary.*temporary again.*challenge page",
        ):
            source.search("https://www.avito.ru/test")

    def test_debug_save_preserves_both_transport_pages(self) -> None:
        curl = FakeTransport(RetrievedPage("temporary", 503, "url", "curl_cffi"))
        requests = FakeTransport(fixture_page())
        playwright = FakeTransport(error=AssertionError("must not be called"))
        source = AvitoSource(
            curl_transport=curl,
            requests_transport=requests,
            playwright_transport=playwright,
        )

        with TemporaryDirectory() as directory:
            debug_dir = Path(directory)
            source.search("https://www.avito.ru/test", debug_dir=debug_dir)

            curl_files = list(debug_dir.glob("avito_*_curl_cffi_response.html"))
            requests_files = list(debug_dir.glob("avito_*_requests_response.html"))
            self.assertEqual(len(curl_files), 1)
            self.assertEqual(curl_files[0].read_text(), "temporary")
            self.assertEqual(len(requests_files), 1)

    def test_repeated_debug_runs_do_not_overwrite(self) -> None:
        curl = FakeTransport(RetrievedPage("temporary", 503, "url", "curl_cffi"))
        requests = FakeTransport(fixture_page())
        playwright = FakeTransport(error=AssertionError("must not be called"))
        source = AvitoSource(
            curl_transport=curl,
            requests_transport=requests,
            playwright_transport=playwright,
        )

        with TemporaryDirectory() as directory:
            debug_dir = Path(directory)
            source.search("https://www.avito.ru/test", debug_dir=debug_dir)
            source.search("https://www.avito.ru/test", debug_dir=debug_dir)

            self.assertEqual(
                len(list(debug_dir.glob("avito_*_curl_cffi_response.html"))),
                2,
            )
            self.assertEqual(
                len(list(debug_dir.glob("avito_*_requests_response.html"))),
                2,
            )

    def test_network_json_is_preferred_over_dom(self) -> None:
        raw_item = {
            "id": 123,
            "title": "Network listing",
            "urlPath": "/habarovsk/item_123",
            "priceDetailed": {"value": 1000, "fullString": "1 000 ₽"},
        }
        playwright = FakeTransport(
            RetrievedPage(
                html="<html></html>",
                status_code=200,
                final_url="https://www.avito.ru/test",
                transport="playwright",
                network_json=[
                    NetworkJsonResponse(
                        "https://www.avito.ru/web/search",
                        200,
                        {"result": {"catalog": {"items": [raw_item]}}},
                    )
                ],
            )
        )
        source = AvitoSource(
            transport_mode="playwright",
            playwright_transport=playwright,
        )

        result = source.search("https://www.avito.ru/test")

        self.assertEqual(result.extraction, "network-json")
        self.assertEqual(result.listings[0].title, "Network listing")

    def test_cli_prints_fallback_transport(self) -> None:
        curl = FakeTransport(error=TransportError("temporary", retryable=True))
        requests = FakeTransport(fixture_page())
        playwright = FakeTransport(error=AssertionError("must not be called"))
        source = AvitoSource(
            curl_transport=curl,
            requests_transport=requests,
            playwright_transport=playwright,
        )
        output = io.StringIO()

        with redirect_stdout(output):
            exit_code = run(
                ["--url", "https://www.avito.ru/test"],
                source=source,
            )

        self.assertEqual(exit_code, 0)
        self.assertIn(
            "Transport: requests (fallback: temporary)",
            output.getvalue(),
        )
        self.assertIn("Extraction: embedded-json", output.getvalue())

    def test_page_problem_identifies_captcha(self) -> None:
        reason = describe_page_problem(200, "<html>captcha</html>")

        self.assertIn("challenge page", reason or "")

    def test_playwright_transport_keeps_profile_configuration(self) -> None:
        transport = PlaywrightTransport(
            profile_dir=Path("custom-profile"),
            headed=True,
            diagnostic_pause_seconds=7,
        )

        self.assertEqual(transport.profile_dir, Path("custom-profile"))
        self.assertTrue(transport.headed)
        self.assertEqual(transport.diagnostic_pause_seconds, 7)


if __name__ == "__main__":
    unittest.main()
