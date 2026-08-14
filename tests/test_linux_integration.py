from __future__ import annotations

import os
from pathlib import Path
import stat
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from src.gui.service import GuiService
from src.linux_integration import (
    APPLICATION_ID,
    DESKTOP_FILENAME,
    EXECUTABLE_NAME,
    ICON_SIZES,
    IntegrationPaths,
    IntegrationStatus,
    LinuxIntegration,
    _refresh_desktop_caches,
)


ROOT = Path(__file__).parents[1]
STATIC = ROOT / "src/gui/static"


class LinuxIntegrationDetectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "ResellMonitor-0.1.0-x86_64.AppImage"
        self.source.write_bytes(b"appimage")
        self.source.chmod(0o755)
        self.environment = {
            "HOME": str(self.root / "home"),
            "XDG_BIN_HOME": str(self.root / "bin"),
            "XDG_DATA_HOME": str(self.root / "share"),
            "APPIMAGE": str(self.source),
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def integration(self, **overrides: object) -> LinuxIntegration:
        values = {"environment": self.environment, "platform": "linux", "frozen": True}
        values.update(overrides)
        return LinuxIntegration(**values)

    def test_valid_linux_appimage_is_portable_and_installable(self) -> None:
        self.assertEqual(
            self.integration().status(),
            {"status": IntegrationStatus.PORTABLE, "can_install": True},
        )

    def test_no_appimage_is_unavailable(self) -> None:
        environment = dict(self.environment)
        environment.pop("APPIMAGE")
        self.assertEqual(
            self.integration(environment=environment).status()["status"],
            IntegrationStatus.UNAVAILABLE,
        )

    def test_relative_missing_directory_and_control_paths_are_rejected(self) -> None:
        candidates = (
            "relative.AppImage",
            str(self.root / "missing.AppImage"),
            str(self.root),
            str(self.source) + "\n",
        )
        for candidate in candidates:
            environment = {**self.environment, "APPIMAGE": candidate}
            with self.subTest(candidate=candidate):
                self.assertEqual(
                    self.integration(environment=environment).status()["status"],
                    IntegrationStatus.UNAVAILABLE,
                )

    def test_source_onedir_and_non_linux_modes_are_unavailable(self) -> None:
        for platform, frozen in (("linux", False), ("darwin", True)):
            with self.subTest(platform=platform, frozen=frozen):
                self.assertEqual(
                    self.integration(platform=platform, frozen=frozen).status()["status"],
                    IntegrationStatus.UNAVAILABLE,
                )

    def test_malformed_xdg_root_is_unavailable(self) -> None:
        environment = {**self.environment, "XDG_DATA_HOME": "relative/share"}
        self.assertEqual(
            self.integration(environment=environment).status()["status"],
            IntegrationStatus.UNAVAILABLE,
        )


class LinuxIntegrationInstallationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "portable.AppImage"
        self.source.write_bytes(b"current appimage")
        self.source.chmod(0o755)
        self.environment = {
            "HOME": str(self.root / "home"),
            "XDG_BIN_HOME": str(self.root / "bin"),
            "XDG_DATA_HOME": str(self.root / "share"),
            "XDG_CONFIG_HOME": str(self.root / "config"),
            "XDG_CACHE_HOME": str(self.root / "cache"),
            "APPIMAGE": str(self.source),
        }
        self.paths = IntegrationPaths.from_environment(self.environment)
        self.integration = LinuxIntegration(
            environment=self.environment, platform="linux", frozen=True
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def install(self) -> dict[str, object]:
        with patch("src.linux_integration._refresh_desktop_caches"):
            return self.integration.install()

    def test_install_copies_appimage_desktop_entry_and_all_icons(self) -> None:
        result = self.install()
        self.assertEqual(result["status"], IntegrationStatus.INTEGRATED)
        self.assertEqual(self.paths.executable.read_bytes(), self.source.read_bytes())
        self.assertTrue(self.paths.executable.stat().st_mode & stat.S_IXUSR)
        self.assertEqual(self.paths.executable.name, EXECUTABLE_NAME)
        self.assertNotIn("0.1.0", self.paths.executable.name)
        desktop = self.paths.desktop_entry.read_text(encoding="utf-8")
        self.assertIn(f'Exec="{self.paths.executable}"', desktop)
        self.assertIn(f"Icon={APPLICATION_ID}", desktop)
        self.assertEqual(self.paths.desktop_entry.name, DESKTOP_FILENAME)
        self.assertNotIn(str(self.source), desktop)
        for size in ICON_SIZES:
            self.assertEqual(
                self.paths.icon(size).read_bytes(),
                (ROOT / f"assets/branding/resell-monitor-{size}.png").read_bytes(),
            )
        self.assertEqual(self.integration.status()["status"], IntegrationStatus.INTEGRATED)

    def test_reinstall_replaces_stable_executable(self) -> None:
        self.install()
        self.source.write_bytes(b"rebuilt appimage")
        self.source.chmod(0o755)
        self.install()
        self.assertEqual(self.paths.executable.read_bytes(), b"rebuilt appimage")

    def test_install_never_touches_persistent_data_config_or_cache(self) -> None:
        markers = (
            Path(self.environment["XDG_DATA_HOME"]) / "resell-monitor/history",
            Path(self.environment["XDG_CONFIG_HOME"]) / "resell-monitor/searches.json",
            Path(self.environment["XDG_CACHE_HOME"]) / "resell-monitor/state",
        )
        for marker in markers:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text("keep", encoding="utf-8")
        self.install()
        for marker in markers:
            self.assertEqual(marker.read_text(encoding="utf-8"), "keep")

    def test_failed_copy_preserves_existing_executable(self) -> None:
        self.paths.executable.parent.mkdir(parents=True)
        self.paths.executable.write_bytes(b"existing installed image")
        with patch("src.linux_integration.shutil.copyfileobj", side_effect=OSError("full")), \
             patch("src.linux_integration._refresh_desktop_caches"):
            with self.assertRaisesRegex(OSError, "full"):
                self.integration.install()
        self.assertEqual(self.paths.executable.read_bytes(), b"existing installed image")
        self.assertEqual(list(self.paths.executable.parent.glob("*.installing-*")), [])

    def test_destination_symlink_is_rejected_without_following_it(self) -> None:
        outside = self.root / "outside"
        outside.write_bytes(b"outside")
        self.paths.executable.parent.mkdir(parents=True)
        self.paths.executable.symlink_to(outside)
        with self.assertRaisesRegex(OSError, "unsafe"):
            self.install()
        self.assertEqual(outside.read_bytes(), b"outside")

    def test_symlinked_integration_directory_is_rejected(self) -> None:
        outside = self.root / "outside-bin"
        outside.mkdir()
        self.paths.executable.parent.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(OSError, "unsafe"):
            self.install()
        self.assertEqual(list(outside.iterdir()), [])

    def test_cli_style_desktop_entry_is_recognized_as_integrated(self) -> None:
        self.install()
        desktop = self.paths.desktop_entry.read_text(encoding="utf-8")
        desktop = desktop.replace(
            f'Exec="{self.paths.executable}"', f"Exec={self.paths.executable}"
        )
        self.paths.desktop_entry.write_text(desktop, encoding="utf-8")
        self.assertEqual(self.integration.status()["status"], IntegrationStatus.INTEGRATED)


class DesktopIntegrationBoundaryTests(unittest.TestCase):
    def test_optional_cache_refresh_uses_fixed_argv_without_shell(self) -> None:
        applications = Path("/tmp/resell-monitor-test-applications")
        icons = Path("/tmp/resell-monitor-test-icons")
        with patch(
            "src.linux_integration.shutil.which",
            side_effect=("/usr/bin/update-desktop-database", "/usr/bin/gtk-update-icon-cache"),
        ), patch("src.linux_integration.subprocess.run") as run:
            _refresh_desktop_caches(applications, icons)
        self.assertEqual(
            run.call_args_list[0].args[0],
            ["/usr/bin/update-desktop-database", str(applications)],
        )
        self.assertEqual(
            run.call_args_list[1].args[0],
            ["/usr/bin/gtk-update-icon-cache", "-f", "-t", str(icons)],
        )
        for call in run.call_args_list:
            self.assertNotIn("shell", call.kwargs)

    def test_api_is_parameterless_and_has_no_generic_copy_or_shell_bridge(self) -> None:
        app = (ROOT / "src/gui/app.py").read_text(encoding="utf-8")
        module = (ROOT / "src/linux_integration.py").read_text(encoding="utf-8")
        branch = app.split('path == "/api/desktop-integration/install"', 1)[1].split(
            "elif", 1
        )[0]
        self.assertIn("if self._body()", branch)
        self.assertIn("service.install_desktop_integration()", branch)
        self.assertNotIn("source", branch)
        self.assertNotIn("destination", branch)
        self.assertNotIn("shell=True", module)
        self.assertNotIn("install_linux_user.sh", module + app)
        self.assertNotIn("copy_file", app)
        self.assertNotIn("write_file", app)

    def test_gui_and_shell_installer_share_the_fixed_contract(self) -> None:
        installer = (ROOT / "scripts/install_linux_user.sh").read_text(encoding="utf-8")
        module = (ROOT / "src/linux_integration.py").read_text(encoding="utf-8")
        for contract in (
            "XDG_BIN_HOME", "XDG_DATA_HOME", "resell-monitor",
            "applications", "icons/hicolor",
        ):
            self.assertIn(contract, installer)
            self.assertIn(contract, module)
        for size in ICON_SIZES:
            self.assertIn(str(size), installer)
            self.assertIn(str(size), module)
        self.assertNotIn("sudo", module)

    def test_appimage_bundles_desktop_template_and_icons(self) -> None:
        spec = (ROOT / "packaging/ResellMonitor.spec").read_text(encoding="utf-8")
        self.assertIn('"packaging" / "resell-monitor.desktop"', spec)
        self.assertIn('"assets" / "branding"', spec)

    def test_ui_prompt_about_status_dismissal_and_update_hooks(self) -> None:
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        js = (STATIC / "app.js").read_text(encoding="utf-8")
        i18n = (STATIC / "i18n.js").read_text(encoding="utf-8")
        for hook in (
            "integration-prompt", "integration-prompt-install",
            "integration-prompt-dismiss", "about-installation",
            "integration-status", "about-integration-install",
        ):
            self.assertIn(f'id="{hook}"', html)
        self.assertIn('id="integration-prompt"', html)
        self.assertIn("hidden", html.split('id="integration-prompt"', 1)[1].split(">", 1)[0])
        self.assertIn("linux_integration_prompt_dismissed", js)
        self.assertIn("integration.status!=='portable'||dismissed", js)
        self.assertIn("result?.result==='error'", js)
        self.assertIn("result?.result==='installed'", js)
        for update_hook in ("update-status", "check-updates", "view-release"):
            self.assertIn(f'id="{update_hook}"', html)
        self.assertIn('data-section="about" data-editable="false"', html)
        for key in (
            "integration.prompt_title", "integration.add", "integration.not_now",
            "integration.portable", "integration.installed", "integration.error",
            "integration.try_again", "integration.reinstall",
        ):
            self.assertGreaterEqual(i18n.count(f"'{key}'"), 2)

    def test_dismissal_uses_existing_app_settings(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            service = GuiService(
                config_path=root / "searches.json",
                database_path=root / "monitor.db",
                output_dir=root / "output",
                linux_integration=LinuxIntegration(environment={}, frozen=False),
            )
            try:
                self.assertEqual(service.settings()["linux_integration_prompt_dismissed"], 0)
                updated = service.update_settings(
                    {"linux_integration_prompt_dismissed": 1}
                )
                self.assertEqual(updated["linux_integration_prompt_dismissed"], 1)
                self.assertEqual(service.settings()["linux_integration_prompt_dismissed"], 1)
            finally:
                service.close()


if __name__ == "__main__":
    unittest.main()
