from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.gui.service import GuiService


ROOT = Path(__file__).parents[1]


class LocalizationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.catalog = (ROOT / "src/gui/static/i18n.js").read_text(encoding="utf-8")
        cls.script = (ROOT / "src/gui/static/app.js").read_text(encoding="utf-8")
        cls.html = (ROOT / "src/gui/static/index.html").read_text(encoding="utf-8")
        cls.styles = (ROOT / "src/gui/static/styles.css").read_text(encoding="utf-8")

    def test_language_preference_persists_for_russian_and_english(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            service = GuiService(config_path=base / "searches.json",
                                 database_path=base / "db.sqlite", output_dir=base / "out")
            self.assertEqual(service.settings()["interface_language"], "en")
            self.assertEqual(service.update_settings({"interface_language": "ru"})["interface_language"], "ru")
            service.close()
            reopened = GuiService(config_path=base / "searches.json",
                                  database_path=base / "db.sqlite", output_dir=base / "out")
            self.assertEqual(reopened.settings()["interface_language"], "ru")
            self.assertEqual(reopened.update_settings({"interface_language": "en"})["interface_language"], "en")
            reopened.close()

    def test_language_change_is_settings_only_and_never_scans(self) -> None:
        calls: list[object] = []
        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            service = GuiService(config_path=base / "searches.json",
                                 database_path=base / "db.sqlite", output_dir=base / "out",
                                 scan_runner=lambda searches, progress: calls.append(searches) or [])
            service.update_settings({"interface_language": "ru"})
            service.update_settings({"interface_language": "en"})
            self.assertEqual(calls, [])
            service.close()

    def test_translation_lookup_and_safe_english_fallback(self) -> None:
        self.assertIn("const t=(key,params={})", self.catalog)
        self.assertIn("catalogs[language]?.[key]??en[key]??''", self.catalog)
        self.assertIn("'nav.settings':'Настройки'", self.catalog)
        self.assertIn("'nav.settings':'Settings'", self.catalog)
        self.assertNotIn("??key", self.catalog)

    def test_major_navigation_and_settings_navigation_are_translated(self) -> None:
        for value in ("Обзор", "Поиски", "Объявления", "Рынок", "История",
                      "Источники", "Настройки"):
            self.assertIn(f"'{value}'", self.catalog)
        for value in ("Основные", "Оформление", "Язык", "Данные и хранилище",
                      "Конфиденциальность и безопасность", "Поддержка", "О программе"):
            self.assertIn(f"'{value}'", self.catalog)

    def test_search_editor_catalog_and_existing_mode_behavior(self) -> None:
        for value in ("Шаблон", "Источник", "Местоположение", "Цена от", "Цена до",
                      "Профиль запроса", "Режим сессии", "Сохранить поиск", "Отмена"):
            self.assertIn(f"'{value}'", self.catalog)
        self.assertIn("classList.toggle('simple-editor'", self.script)
        self.assertIn("updateSourceControls()", self.script)
        self.assertIn("i18n.js", self.html)

    def test_language_control_is_finished_and_only_lists_supported_languages(self) -> None:
        self.assertIn('data-setting="interface_language"', self.html)
        self.assertIn('<option value="ru">Русский</option>', self.html)
        self.assertIn('<option value="en">English</option>', self.html)
        self.assertIn("I18n.setLanguage(settings.interface_language)", self.script)
        self.assertEqual(self.html.count('data-i18n="settings.interface_language"'), 1)
        self.assertNotIn("label:has(#interface-language)", self.catalog)
        self.assertIn("Choose the language used by the application interface.", self.catalog)
        self.assertIn("Выберите язык интерфейса приложения.", self.catalog)

    def test_language_preview_does_not_reload_unsaved_settings(self) -> None:
        handler = "$('#interface-language').onchange=e=>{I18n.setLanguage(e.target.value);refreshPageTitle();renderProjectInfo()}"
        self.assertIn(handler, self.script)
        self.assertNotIn(handler[:-1] + ";loadPage(state.page)}", self.script)
        self.assertIn("state.settings=await api('/api/settings'", self.script)
        self.assertIn("I18n.setLanguage(state.settings.interface_language)", self.script)

    def test_cyrillic_layout_safety_rules_exist(self) -> None:
        self.assertIn("grid-template-columns:minmax(220px,260px)", self.styles)
        self.assertIn("overflow-wrap:anywhere", self.styles)
        self.assertIn("white-space:nowrap", self.styles)
        self.assertIn("flex-wrap:wrap", self.styles)

    def test_internal_technical_names_remain_untranslated(self) -> None:
        for name in ("Avito", "FarPost", "Youla", "HTTP", "SQLite", "JSON", "GPU", "CPU", "SSD"):
            self.assertIn(name, self.html + self.catalog)

    def test_cleanup_ui_has_english_and_russian_catalog_entries(self) -> None:
        for key in ("cleanup.automatic", "cleanup.inbox_lifetime",
                    "cleanup.disappear_after", "cleanup.archive_after",
                    "cleanup.retention", "cleanup.remove_inbox", "cleanup.archive"):
            self.assertGreaterEqual(self.catalog.count(f"'{key}'"), 2)
        for text in ("Автоматическая очистка", "Срок в очереди",
                     "Убрать старые из очереди", "Архивировать исчезнувшие"):
            self.assertIn(f"'{text}'", self.catalog)
