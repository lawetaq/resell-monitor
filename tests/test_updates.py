from __future__ import annotations

import json
from pathlib import Path
import threading
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from src.gui.service import GuiService
from src.project import GITHUB_RELEASES_API_URL, REPOSITORY_URL
from src.updates import (
    UPDATE_TIMEOUT_SECONDS,
    _fetch_releases,
    check_for_updates,
    parse_release_version,
)


def release(tag: str, *, url: str | None = None, draft: bool = False) -> dict[str, object]:
    return {
        "tag_name": tag,
        "name": f"Resell Monitor {tag}",
        "html_url": url or f"{REPOSITORY_URL}/releases/tag/{tag}",
        "draft": draft,
    }


class VersionComparisonTests(unittest.TestCase):
    def test_numeric_versions_are_compared_semantically(self) -> None:
        alpha = parse_release_version("v0.1.0-alpha")
        self.assertEqual(alpha, parse_release_version("0.1.0-alpha"))
        self.assertLess(alpha, parse_release_version("v0.1.1-alpha"))
        self.assertLess(alpha, parse_release_version("v0.2.0-alpha"))
        self.assertLess(alpha, parse_release_version("v1.0.0"))
        self.assertLess(parse_release_version("v0.3.0-alpha"), parse_release_version("v0.3.0-beta"))
        self.assertLess(parse_release_version("v0.3.0-beta"), parse_release_version("v0.3.0"))

    def test_malformed_or_unrelated_tags_are_rejected(self) -> None:
        for tag in ("latest", "release-0.2", "v0.2.0-rc1", "v01.2.3", "v0.2.0-alpha-extra", ""):
            with self.subTest(tag=tag):
                self.assertIsNone(parse_release_version(tag))


class UpdateProviderTests(unittest.TestCase):
    def test_same_version_is_up_to_date(self) -> None:
        result = check_for_updates(lambda: [release("v0.1.0-alpha")])
        self.assertEqual(result["status"], "up_to_date")
        self.assertNotIn("release_url", result)

    def test_newer_patch_minor_major_and_prerelease_are_updates(self) -> None:
        for tag in ("v0.1.1-alpha", "v0.2.0-alpha", "v1.0.0", "v0.1.0-beta"):
            with self.subTest(tag=tag):
                result = check_for_updates(lambda tag=tag: [release(tag)])
                self.assertEqual(result["status"], "update_available")
                self.assertEqual(result["release_url"], f"{REPOSITORY_URL}/releases/tag/{tag}")

    def test_highest_valid_non_draft_project_release_is_selected(self) -> None:
        result = check_for_updates(lambda: [
            release("not-a-version"),
            release("v9.0.0", draft=True),
            release("v8.0.0", url="https://example.com/releases/tag/v8.0.0"),
            release("v0.2.0-alpha"),
            release("v0.3.0-beta"),
        ])
        self.assertEqual(result["latest_version"], "0.3.0-beta")
        self.assertEqual(result["latest_channel"], "beta")

    def test_no_releases_timeout_api_error_and_malformed_response(self) -> None:
        self.assertEqual(check_for_updates(lambda: [release("invalid")])["status"], "no_release")
        self.assertEqual(check_for_updates(lambda: [])["status"], "no_release")
        for error in (TimeoutError("offline"), OSError("network"), ValueError("bad json")):
            def fail(error: Exception = error) -> object:
                raise error
            self.assertEqual(check_for_updates(fail)["status"], "api_error")
        self.assertEqual(check_for_updates(lambda: {"message": "rate limited"})["status"], "api_error")

    def test_request_is_fixed_bounded_and_contains_no_user_data(self) -> None:
        class Response:
            def __enter__(self) -> Response:
                return self

            def __exit__(self, *args: object) -> None:
                return None

            def read(self, _size: int) -> bytes:
                return b"[]"

        captured: list[tuple[object, float]] = []

        def fake_open(request: object, *, timeout: float) -> Response:
            captured.append((request, timeout))
            return Response()

        with patch("src.updates.urlopen", side_effect=fake_open):
            self.assertEqual(_fetch_releases(), [])
        request, timeout = captured[0]
        self.assertEqual(request.full_url, GITHUB_RELEASES_API_URL)
        self.assertEqual(request.method, "GET")
        self.assertEqual(timeout, UPDATE_TIMEOUT_SECONDS)
        self.assertIsNone(request.data)
        serialized = json.dumps(dict(request.header_items())).casefold()
        for private_field in ("search", "listing", "location", "identifier", "telemetry"):
            self.assertNotIn(private_field, serialized)


class UpdateServiceTests(unittest.TestCase):
    def test_construction_and_project_info_do_not_check_for_updates(self) -> None:
        calls: list[str] = []
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            service = GuiService(
                config_path=root / "searches.json",
                database_path=root / "database.sqlite",
                output_dir=root / "output",
                update_checker=lambda: calls.append("check") or {"status": "up_to_date"},
            )
            self.assertEqual(service.project_info()["version"], "0.1.0")
            self.assertEqual(calls, [])
            self.assertEqual(service.check_for_updates()["status"], "up_to_date")
            self.assertEqual(calls, ["check"])
            service.close()

    def test_in_flight_check_is_not_duplicated_and_failure_is_normalized(self) -> None:
        entered = threading.Event()
        release_check = threading.Event()

        def slow_check() -> dict[str, object]:
            entered.set()
            release_check.wait(2)
            return {"status": "up_to_date"}

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            service = GuiService(
                config_path=root / "searches.json",
                database_path=root / "database.sqlite",
                output_dir=root / "output",
                update_checker=slow_check,
            )
            result: list[dict[str, object]] = []
            thread = threading.Thread(target=lambda: result.append(service.check_for_updates()))
            thread.start()
            self.assertTrue(entered.wait(1))
            self.assertEqual(service.check_for_updates()["status"], "checking")
            release_check.set()
            thread.join(2)
            self.assertEqual(result[0]["status"], "up_to_date")
            service._update_checker = lambda: (_ for _ in ()).throw(RuntimeError("private failure"))
            with self.assertLogs("src.gui.service", level="WARNING"):
                self.assertEqual(service.check_for_updates()["status"], "api_error")
            self.assertIsNone(service.runtime()["last_error"])
            service.close()


if __name__ == "__main__":
    unittest.main()
