from __future__ import annotations

import os
from pathlib import Path
import stat
import subprocess
from tempfile import TemporaryDirectory
import unittest
import xml.etree.ElementTree as ET

from src.version import __version__


ROOT = Path(__file__).parents[1]


class AppStreamContractTests(unittest.TestCase):
    def test_metadata_is_factual_and_matches_desktop_entry(self) -> None:
        metadata_path = ROOT / "packaging/resell-monitor.metainfo.xml"
        component = ET.parse(metadata_path).getroot()
        self.assertEqual(component.tag, "component")
        self.assertEqual(component.findtext("id"), "io.github.lawetaq.ResellMonitor")
        self.assertEqual(component.findtext("name"), "Resell Monitor")
        launchable = component.find("launchable")
        self.assertIsNotNone(launchable)
        self.assertEqual(launchable.get("type"), "desktop-id")
        self.assertEqual(launchable.text, "resell-monitor.desktop")
        homepage = component.find("url")
        self.assertEqual(homepage.get("type"), "homepage")
        self.assertEqual(homepage.text, "https://github.com/lawetaq/resell-monitor")
        self.assertIsNone(component.find("screenshots"))
        self.assertIsNone(component.find("releases"))


class LinuxUserIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.home = self.root / "home"
        self.data_home = self.home / ".local/share"
        self.bin_home = self.home / ".local/bin"
        self.config_home = self.home / ".config"
        self.cache_home = self.home / ".cache"
        self.home.mkdir()
        self.appimage = self.root / f"ResellMonitor-{__version__}-x86_64.AppImage"
        self.appimage.write_bytes(b"first artifact")
        self.appimage.chmod(0o755)
        self.environment = {
            **os.environ,
            "HOME": str(self.home),
            "XDG_DATA_HOME": str(self.data_home),
            "XDG_BIN_HOME": str(self.bin_home),
            "XDG_CONFIG_HOME": str(self.config_home),
            "XDG_CACHE_HOME": str(self.cache_home),
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _run(self, script: str, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(ROOT / "scripts" / script), *arguments],
            cwd=ROOT,
            env=self.environment,
            check=True,
            text=True,
            capture_output=True,
        )

    def test_install_update_and_uninstall_preserve_user_data(self) -> None:
        persistent_files = [
            self.data_home / "resell-monitor/resell-monitor.db",
            self.config_home / "resell-monitor/searches.json",
            self.cache_home / "resell-monitor/marker",
        ]
        for path in persistent_files:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("keep", encoding="utf-8")

        self._run("install_linux_user.sh", str(self.appimage))
        executable = self.bin_home / "resell-monitor"
        desktop = self.data_home / "applications/resell-monitor.desktop"
        self.assertEqual(executable.read_bytes(), b"first artifact")
        self.assertTrue(executable.stat().st_mode & stat.S_IXUSR)
        desktop_text = desktop.read_text(encoding="utf-8")
        self.assertIn(f"Exec={executable}", desktop_text)
        self.assertIn("Icon=resell-monitor", desktop_text)
        self.assertNotIn(str(ROOT), desktop_text)
        for size in (32, 48, 64, 128, 256, 512):
            self.assertTrue(
                (self.data_home / f"icons/hicolor/{size}x{size}/apps/resell-monitor.png").is_file()
            )

        self.appimage.write_bytes(b"newer artifact")
        self.appimage.chmod(0o755)
        self._run("install_linux_user.sh", str(self.appimage))
        self.assertEqual(executable.read_bytes(), b"newer artifact")
        self._run("uninstall_linux_user.sh")
        self.assertFalse(executable.exists())
        self.assertFalse(desktop.exists())
        for path in persistent_files:
            self.assertEqual(path.read_text(encoding="utf-8"), "keep")

    def test_scripts_are_user_scoped_and_never_invoke_sudo(self) -> None:
        installer = (ROOT / "scripts/install_linux_user.sh").read_text()
        uninstaller = (ROOT / "scripts/uninstall_linux_user.sh").read_text()
        self.assertIn('.local/bin', installer)
        self.assertNotIn("sudo", installer + uninstaller)
        for persistent in (
            ".local/share/resell-monitor",
            ".config/resell-monitor",
            ".cache/resell-monitor",
        ):
            self.assertNotIn(persistent, uninstaller)


class ReleaseContractTests(unittest.TestCase):
    def test_release_sources_and_canonical_naming_exist(self) -> None:
        build = (ROOT / "scripts/build_release_linux.sh").read_text()
        validation = (ROOT / "scripts/validate_release_linux.sh").read_text()
        self.assertIn("from src.version import __version__", build)
        self.assertIn("from src.version import RELEASE_CHANNEL", build)
        self.assertIn("scripts/build_appimage.sh", build)
        self.assertIn('docs/releases/${version}-${release_channel}.md', build)
        self.assertIn('sha256sum "$artifact_name"', build)
        self.assertIn("ResellMonitor-${version}-${architecture}.AppImage", build)
        self.assertIn("sha256sum -c", validation)
        self.assertIn("from src.version import RELEASE_CHANNEL", validation)
        self.assertIn("Release artifact is stale", validation)
        self.assertIn("searches.json", validation)
        self.assertTrue((ROOT / "docs/releases/0.1.0-alpha.md").is_file())
        self.assertTrue((ROOT / "CHANGELOG.md").is_file())

    def test_first_alpha_publication_contract(self) -> None:
        releasing = (ROOT / "docs/RELEASING.md").read_text(encoding="utf-8")
        notes = (ROOT / "docs/releases/0.1.0-alpha.md").read_text(encoding="utf-8")
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        for expected in (
            "v0.1.0-alpha", "Resell Monitor 0.1.0 Alpha", "Pre-release",
            "ResellMonitor-0.1.0-x86_64.AppImage",
            "ResellMonitor-0.1.0-x86_64.AppImage.sha256",
        ):
            self.assertIn(expected, releasing)
        for heading in (
            "## Highlights", "## Download", "## Installation", "## Updating",
            "## Data and privacy", "## Known limitations", "## Verification",
        ):
            self.assertIn(heading, notes)
        self.assertLess(changelog.index("## [Unreleased]"), changelog.index("## [0.1.0]"))
        self.assertIn("first public alpha", notes.casefold())

    def test_public_media_and_external_tester_guidance(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        media = (ROOT / "docs/media/README.md").read_text(encoding="utf-8")
        tester = (ROOT / "docs/TESTING_ALPHA.md").read_text(encoding="utf-8")
        for filename in (
            "overview-dark.png", "listings-dark.png", "about-dark.png",
            "theme-switching.gif", "search-workflow.gif",
        ):
            self.assertIn(filename, media)
            self.assertNotIn(f"](docs/media/{filename})", readme)
        self.assertIn("1400×880", media)
        self.assertIn("Linux distribution/version:", tester)
        self.assertIn("cookies, credentials, databases", tester.casefold())

    def test_documentation_contract(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        for heading in ("## Installation", "## Updating", "## Data and privacy"):
            self.assertIn(heading, readme)
        self.assertIn("Alpha", readme)
        self.assertIn("Linux x86_64", readme)
        self.assertNotIn("Windows support", readme)
        self.assertIn("Windows packaging", readme)
        self.assertTrue((ROOT / "docs/media/README.md").is_file())


if __name__ == "__main__":
    unittest.main()
