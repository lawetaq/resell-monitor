from __future__ import annotations

import argparse
from http.cookiejar import CookieJar
import importlib
import json
from pathlib import Path
import socket
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, build_opener, urlopen

from src.desktop import (
    DesktopDependencyError,
    LOOPBACK_HOST,
    launch_desktop,
    load_webview,
    new_session_token,
    run,
    validate_external_url,
)
from src.gui.app import create_server


class FakeWebview:
    def __init__(self, *, fail_window: bool = False) -> None:
        self.fail_window = fail_window
        self.windows: list[tuple[tuple[object, ...], dict[str, object]]] = []
        self.starts: list[dict[str, object]] = []

    def create_window(self, *args: object, **kwargs: object) -> object:
        self.windows.append((args, kwargs))
        if self.fail_window:
            raise RuntimeError("window failed")
        return object()

    def start(self, **kwargs: object) -> None:
        self.starts.append(kwargs)


class DesktopShellTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.config = self.root / "searches.json"
        self.database = self.root / "monitor.sqlite"
        self.output = self.root / "output"
        self.config.write_text("[]\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def args(self) -> argparse.Namespace:
        return argparse.Namespace(
            config=self.config,
            database=self.database,
            output_dir=self.output,
            debug_dir=None,
            debug=False,
            gui=None,
        )

    def test_desktop_requirements_select_qt_only_on_linux(self) -> None:
        project = Path(__file__).parents[1]
        desktop = (project / "requirements-desktop.txt").read_text(encoding="utf-8")
        browser = (project / "requirements.txt").read_text(encoding="utf-8")
        readme = (project / "README.md").read_text(encoding="utf-8")
        self.assertIn('-r requirements.txt', desktop)
        self.assertIn('pywebview[qt]>=5,<7; sys_platform == "linux"', desktop)
        self.assertIn('pywebview>=5,<7; sys_platform != "linux"', desktop)
        self.assertNotIn("pywebview", browser)
        self.assertIn("pip install -r requirements-desktop.txt", readme)
        self.assertIn("python -m src.desktop --gui qt", readme)
        self.assertIn("python -m src.desktop --gui gtk", readme)

    def test_loopback_ephemeral_port_and_desktop_session_protection(self) -> None:
        token = new_session_token()
        server = create_server(
            config_path=self.config,
            database_path=self.database,
            output_dir=self.output,
            host=LOOPBACK_HOST,
            port=0,
            desktop_session_token=token,
        )
        server.start()
        try:
            self.assertEqual(server._httpd.server_address[0], LOOPBACK_HOST)
            self.assertGreater(server.port, 0)
            self.assertNotEqual(server.port, 8765)
            for path in ("/", "/api/runtime"):
                with self.subTest(path=path), self.assertRaises(HTTPError) as caught:
                    urlopen(server.url + path)
                self.assertEqual(caught.exception.code, 401)
            with self.assertRaises(HTTPError) as caught:
                urlopen(server.url + "/desktop/invalid/")
            self.assertEqual(caught.exception.code, 401)

            opener = build_opener(HTTPCookieProcessor(CookieJar()))
            response = opener.open(server.url + f"/desktop/{token}/")
            self.assertEqual(response.status, 200)
            self.assertIn(b"Resell Monitor", response.read())
            runtime = json.load(opener.open(server.url + "/api/runtime"))
            self.assertEqual(runtime["state"], "Idle")
            self.assertFalse(runtime["monitoring"])
        finally:
            port = server.port
            server.stop()
        with socket.socket() as client:
            client.settimeout(0.2)
            self.assertNotEqual(client.connect_ex((LOOPBACK_HOST, port)), 0)

    def test_browser_development_server_needs_no_desktop_token(self) -> None:
        server = create_server(
            config_path=self.config,
            database_path=self.database,
            output_dir=self.output,
            port=0,
        )
        server.start()
        try:
            self.assertEqual(urlopen(server.url + "/").status, 200)
            self.assertEqual(json.load(urlopen(server.url + "/api/runtime"))["state"], "Idle")
        finally:
            server.stop()

    def test_tokens_are_random_and_never_persisted(self) -> None:
        first, second = new_session_token(), new_session_token()
        self.assertNotEqual(first, second)
        self.assertGreaterEqual(len(first), 32)
        server = create_server(
            config_path=self.config,
            database_path=self.database,
            output_dir=self.output,
            port=0,
            desktop_session_token=first,
        )
        server.stop()
        self.assertNotIn(first, self.config.read_text(encoding="utf-8"))
        self.assertNotIn(first, self.database.read_bytes().decode("latin-1"))

    def test_external_url_boundary(self) -> None:
        for url in ("https://www.avito.ru/item_1", "http://example.com/item"):
            self.assertEqual(validate_external_url(url), url)
        for url in (
            "javascript:alert(1)", "file:///tmp/a", "data:text/plain,a",
            "http://localhost/a", "http://127.0.0.1:8765/a",
            "http://10.0.0.1/a", "https://user:secret@example.com/a",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_external_url(url, internal_origin="http://127.0.0.1:8765")

    def test_mocked_desktop_startup_has_no_scan_and_shuts_down(self) -> None:
        fake = FakeWebview()
        with patch("src.gui.service._build_source", side_effect=AssertionError("marketplace source created")):
            self.assertEqual(launch_desktop(self.args(), fake), 0)
        self.assertEqual(len(fake.windows), 1)
        positional, options = fake.windows[0]
        self.assertEqual(positional[0], "Resell Monitor")
        self.assertIn("/desktop/", str(positional[1]))
        self.assertEqual(options["min_size"], (1100, 700))
        self.assertTrue(options["resizable"])
        self.assertEqual(fake.starts[0]["debug"], False)
        self.assertEqual(
            fake.starts[0]["icon"],
            str(Path(__file__).parents[1] / "assets/branding/resell-monitor-256.png"),
        )

    def test_desktop_reuses_language_theme_and_listing_image_frontend(self) -> None:
        app = (Path(__file__).parents[1] / "src/gui/static/app.js").read_text()
        self.assertIn("window.pywebview?.api?.open_external_url", app)
        self.assertIn("I18n.setLanguage(state.settings.interface_language)", app)
        self.assertIn("applyAppearance(state.settings)", app)
        self.assertIn("listingImage(l.primary_image_url,'detail')", app)
        self.assertIn('loading="lazy"', app)

    def test_window_creation_failure_still_cleans_up_server(self) -> None:
        fake = FakeWebview(fail_window=True)
        captured_port: list[int] = []
        real_create = create_server

        def recording_create(**kwargs: object):
            server = real_create(**kwargs)
            captured_port.append(server.port)
            return server

        with patch("src.desktop.create_server", side_effect=recording_create):
            with self.assertRaisesRegex(RuntimeError, "window failed"):
                launch_desktop(self.args(), fake)
        with socket.socket() as client:
            client.settimeout(0.2)
            self.assertNotEqual(client.connect_ex((LOOPBACK_HOST, captured_port[0])), 0)

    def test_missing_pywebview_is_a_controlled_error(self) -> None:
        with patch.object(importlib, "import_module", side_effect=ImportError("missing")):
            with self.assertRaisesRegex(DesktopDependencyError, "requirements-desktop"):
                load_webview()
            self.assertEqual(run([]), 2)


if __name__ == "__main__":
    unittest.main()
