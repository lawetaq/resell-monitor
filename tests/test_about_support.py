from __future__ import annotations

from pathlib import Path
import unittest

from src.desktop import validate_external_url
from src.project import project_info
from src.version import RELEASE_CHANNEL, __version__


ROOT = Path(__file__).parents[1]
STATIC = ROOT / "src/gui/static"


class SettingsReadOnlyContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = (STATIC / "index.html").read_text(encoding="utf-8")
        cls.js = (STATIC / "app.js").read_text(encoding="utf-8")
        cls.css = (STATIC / "styles.css").read_text(encoding="utf-8")
        cls.i18n = (STATIC / "i18n.js").read_text(encoding="utf-8")

    def test_save_action_uses_explicit_section_capability_metadata(self) -> None:
        for section in ("about", "support", "sources", "privacy"):
            self.assertIn(f'data-section="{section}" data-editable="false"', self.html)
        for section in ("general", "search", "appearance", "language", "data"):
            self.assertIn(f'data-section="{section}" data-editable="true"', self.html)
        self.assertIn("active?.dataset.editable!=='true'", self.js)
        self.assertIn("$('#settings-actions').hidden", self.js)
        self.assertIn(".settings-actions[hidden]{display:none}", self.css)
        self.assertNotIn("textContent==='About'", self.js)

    def test_about_is_branded_versioned_and_factual(self) -> None:
        about = self.html.split('data-section="about"', 1)[1].split(
            '<div class="settings-actions"', 1
        )[0]
        self.assertIn("about-brand-mark", about)
        self.assertIn('id="about-version"', about)
        self.assertIn('data-i18n="release.alpha"', about)
        for link in ("repository", "releases", "changelog", "license"):
            self.assertIn(f'data-project-link="{link}"', about)
        for fabricated in ("support@", "Discord", "Telegram", "Inc.", "LLC"):
            self.assertNotIn(fabricated, about)
        self.assertNotIn(__version__, about)
        self.assertIn("state.project.version", self.js)

    def test_about_and_support_share_settings_layout_primitives(self) -> None:
        support = self.html.split('data-section="support"', 1)[1].split(
            'data-section="about"', 1
        )[0]
        about = self.html.split('data-section="about"', 1)[1].split(
            '<div class="settings-actions"', 1
        )[0]
        self.assertIn(".settings-section.panel{padding:var(--space-4)}", self.css)
        self.assertNotIn("--space-5", self.css)
        for section in (support, about):
            self.assertIn('class="settings-resource-list"', section)
            self.assertIn('class="settings-resource-row"', section)
        self.assertNotIn('data-i18n="about.version_group"', about)

    def test_resource_rows_remain_semantic_and_accessible(self) -> None:
        settings_resources = self.html.split('data-section="support"', 1)[1].split(
            '<div class="settings-actions"', 1
        )[0]
        self.assertNotIn('<div class="settings-resource-row"', settings_resources)
        self.assertEqual(
            settings_resources.count('class="settings-resource-row" type="button"'),
            7,
        )
        self.assertEqual(settings_resources.count('aria-hidden="true">↗'), 7)
        self.assertIn(":focus-visible", self.css)

    def test_support_is_read_only_and_github_based(self) -> None:
        support = self.html.split('data-section="support"', 1)[1].split(
            'data-section="about"', 1
        )[0]
        for link in ("repository", "issues", "releases"):
            self.assertIn(f'data-project-link="{link}"', support)
        self.assertIn('id="copy-diagnostics"', support)
        self.assertIn("settings-diagnostics", support)
        self.assertNotIn("logs", support.casefold())

    def test_update_dom_contract_is_preserved(self) -> None:
        about = self.html.split('data-section="about"', 1)[1].split(
            '<div class="settings-actions"', 1
        )[0]
        for hook in ("update-status", "check-updates", "view-release"):
            self.assertIn(f'id="{hook}"', about)
        self.assertIn('role="status" aria-live="polite"', about)
        self.assertIn("overflow-wrap:anywhere", self.css)

    def test_new_content_has_english_and_russian_catalog_entries(self) -> None:
        for key in (
            "support.repository", "support.report_issue", "about.description",
            "about.release_channel", "release.alpha", "updates.check",
            "updates.checking", "updates.up_to_date", "updates.available",
            "updates.view_release", "updates.error", "updates.try_again",
        ):
            self.assertGreaterEqual(self.i18n.count(f"'{key}'"), 2)
        for russian in (
            "Проверить обновления", "Проверка…",
            "У вас установлена актуальная версия.", "Доступно обновление",
            "Открыть страницу релиза", "Не удалось проверить обновления.",
        ):
            self.assertIn(russian, self.i18n)

    def test_check_is_manual_disabled_in_flight_and_not_started_on_load(self) -> None:
        self.assertEqual(self.js.count("api('/api/updates/check'"), 1)
        self.assertIn("if(state.updateChecking)return", self.js)
        self.assertIn("button.disabled=state.updateChecking", self.js)
        self.assertIn("$('#check-updates').onclick=()=>checkForUpdates()", self.js)
        load_settings = self.js.split("async function loadSettings()", 1)[1].split(
            "function renderProjectInfo", 1
        )[0]
        self.assertNotIn("/api/updates/check", load_settings)


class ProjectSecurityContractTests(unittest.TestCase):
    def test_project_metadata_is_centralized_and_public_urls_are_safe(self) -> None:
        info = project_info()
        self.assertEqual(info["version"], __version__)
        self.assertEqual(info["release_channel"], RELEASE_CHANNEL)
        for url in info["urls"].values():
            self.assertEqual(validate_external_url(url), url)
        frontend = (STATIC / "app.js").read_text() + (STATIC / "index.html").read_text()
        self.assertNotIn("github.com/lawetaq", frontend)

    def test_external_policy_rejects_non_public_destinations(self) -> None:
        for url in (
            "http://localhost/release", "https://127.0.0.1/release",
            "http://10.0.0.1/release", "file:///tmp/release",
            "javascript:alert(1)", "https://user:pass@github.com/release",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_external_url(url)

    def test_update_endpoint_has_no_frontend_url_parameter(self) -> None:
        app = (ROOT / "src/gui/app.py").read_text()
        update_branch = app.split('path == "/api/updates/check"', 1)[1].split(
            "elif", 1
        )[0]
        self.assertIn("service.check_for_updates()", update_branch)
        self.assertNotIn("self._body", update_branch)


if __name__ == "__main__":
    unittest.main()
