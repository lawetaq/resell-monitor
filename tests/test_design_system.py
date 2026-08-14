from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.gui.service import GuiService


ROOT = Path(__file__).parents[1]


class DesignSystemTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        static = ROOT / "src/gui/static"
        cls.html = (static / "index.html").read_text(encoding="utf-8")
        cls.css = (static / "styles.css").read_text(encoding="utf-8")
        cls.js = (static / "app.js").read_text(encoding="utf-8")
        cls.i18n = (static / "i18n.js").read_text(encoding="utf-8")

    def test_appearance_and_language_persist_together(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            service = GuiService(config_path=base / "searches.json", database_path=base / "db.sqlite", output_dir=base / "out")
            service.update_settings({"appearance_mode": "light", "color_theme": "plum", "interface_language": "ru"})
            service.close()
            reopened = GuiService(config_path=base / "searches.json", database_path=base / "db.sqlite", output_dir=base / "out")
            self.assertEqual(reopened.settings()["appearance_mode"], "light")
            self.assertEqual(reopened.settings()["color_theme"], "plum")
            self.assertEqual(reopened.settings()["interface_language"], "ru")
            reopened.close()

    def test_all_modes_and_color_families_are_real_controls(self) -> None:
        for mode in ("system", "light", "dark"):
            self.assertIn(f'data-appearance-mode="{mode}"', self.html)
        for theme in ("graphite", "moss", "ember", "plum"):
            self.assertIn(f'data-color-theme="{theme}"', self.html)
            self.assertIn(f'[data-theme="{theme}"][data-mode="dark"]', self.css)
            self.assertIn(f'[data-theme="{theme}"][data-mode="light"]', self.css)
        self.assertIn("prefers-color-scheme: dark", self.js)
        self.assertIn("applyAppearance(settings)", self.js)

    def test_semantic_tokens_and_quiet_surface_policy(self) -> None:
        for token in ("--bg-app", "--bg-sidebar", "--bg-surface", "--text-primary", "--border-normal", "--accent", "--success", "--warning", "--danger"):
            self.assertIn(token, self.css)
        self.assertNotIn("linear-gradient", self.css)
        self.assertNotIn("radial-gradient", self.css)
        self.assertIn("--radius-modal:12px", self.css)
        self.assertIn("--sidebar-width:192px", self.css)

    def test_appearance_strings_are_localized(self) -> None:
        for key in ("appearance.mode", "appearance.system", "appearance.light", "appearance.dark", "appearance.color_theme", "appearance.graphite", "appearance.moss", "appearance.ember", "appearance.plum"):
            self.assertGreaterEqual(self.i18n.count(f"'{key}'"), 2)
        for value in ("Системный", "Светлый", "Тёмный", "Графит", "Мох", "Уголь", "Слива"):
            self.assertIn(f"'{value}'", self.i18n)

    def test_existing_interactions_keep_their_contracts(self) -> None:
        self.assertIn("classList.toggle('simple-editor'", self.js)
        self.assertIn("updateSourceControls()", self.js)
        self.assertIn("sort_by", self.js)
        self.assertIn("data-settings-section", self.html)
        self.assertIn("#listing-dialog", self.css)

    def test_search_switch_is_accessible_horizontal_toggle(self) -> None:
        self.assertIn('role="switch"', self.js)
        self.assertIn('aria-checked="${s.enabled?', self.js)
        self.assertIn("setAttribute('aria-checked',String(x.checked))", self.js)
        for rule in ("width:46px", "height:24px", "width:18px", "transform:translateX(22px)"):
            self.assertIn(rule, self.css)

    def test_settings_save_is_a_compact_action_row(self) -> None:
        self.assertIn('<div class="settings-actions" id="settings-actions"><button class="btn primary" id="save-settings"', self.html)
        self.assertIn(".settings-actions{display:flex;justify-content:flex-end", self.css)
        self.assertIn(".settings-actions[hidden]{display:none}", self.css)
        self.assertIn("'settings.save':'Save changes'", self.i18n)
        self.assertIn("'settings.save':'Сохранить изменения'", self.i18n)

    def test_listings_workspace_has_stable_scroll_viewport(self) -> None:
        self.assertIn("#page-listings.active{display:flex", self.css)
        self.assertIn("min-height:clamp(320px,calc(100dvh - 360px),720px)", self.css)
        self.assertIn("max-height:clamp(320px,calc(100dvh - 360px),720px)", self.css)
        self.assertIn(".table-panel>.table-wrap{min-height:0;overflow:auto}", self.css)
        self.assertIn("#listing-table tr{height:auto}", self.css)

    def test_archived_only_selection_disables_inbox_removal(self) -> None:
        self.assertIn("dismiss.disabled=!selectedRows.some(row=>row.in_working_inbox)", self.js)
        self.assertIn("if(action==='dismiss')selected=selected.filter", self.js)
        self.assertIn("dismiss.setAttribute('aria-disabled'", self.js)

    def test_ui_only_preferences_do_not_start_scans(self) -> None:
        calls: list[object] = []
        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            service = GuiService(config_path=base / "searches.json", database_path=base / "db.sqlite", output_dir=base / "out", scan_runner=lambda searches, progress: calls.append(searches) or [])
            service.update_settings({"appearance_mode": "system", "color_theme": "ember"})
            self.assertEqual(calls, [])
            service.close()


if __name__ == "__main__":
    unittest.main()
