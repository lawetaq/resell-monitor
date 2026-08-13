from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from bs4 import BeautifulSoup

from src.config import load_searches, save_searches
from src.gui.service import GuiService
from src.models import SearchConfig


ROOT = Path(__file__).parents[1]


class SearchEditorMarkupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = (ROOT / "src/gui/static/index.html").read_text(encoding="utf-8")
        cls.script = (ROOT / "src/gui/static/app.js").read_text(encoding="utf-8")
        cls.styles = (ROOT / "src/gui/static/styles.css").read_text(encoding="utf-8")
        cls.form = BeautifulSoup(cls.html, "html.parser").select_one("#search-form")
        assert cls.form is not None

    def test_simple_mode_contains_only_product_fields_and_avito_basics(self) -> None:
        always_visible: set[str] = set()
        for label in self.form.select("label:not(.advanced-only):not(.source-control)"):
            control = label.select_one("input[name], select[name], textarea[name], #search-preset, #specific-location")
            if control:
                always_visible.add(str(control.get("name") or control.get("id")))
        self.assertEqual(always_visible, {
            "search-preset", "source", "enabled", "location_mode",
            "specific-location", "min_price", "max_price",
        })
        self.assertIn(".simple-editor .advanced-only{display:none}", self.styles)

    def test_advanced_mode_retains_complete_existing_form(self) -> None:
        names = {str(item.get("name")) for item in self.form.select("[name]")}
        self.assertTrue({
            "name", "url", "interval_seconds", "jitter_seconds",
            "block_retry_delay_seconds", "block_cooldown_seconds", "target_price",
            "max_block_retries", "include_terms", "exclude_terms", "brands",
            "network_route", "proxy_url", "avito_impersonation", "avito_session_mode",
        } <= names)

    def test_switching_mode_only_changes_visibility_and_preserves_values(self) -> None:
        function = self.script.split("function setEditor(mode)", 1)[1].split("function ", 1)[0]
        self.assertIn("classList.toggle('simple-editor'", function)
        self.assertNotIn("reset()", function)
        self.assertNotIn(".value=", function)
        self.assertIn("interval_seconds:num('interval_seconds')", self.script)
        self.assertIn("include_terms:terms('include_terms')", self.script)

    def test_source_specific_controls_are_avito_only_and_user_facing(self) -> None:
        controls = self.form.select("[data-source-control]")
        self.assertEqual([item.get("data-source-control") for item in controls], ["avito", "avito"])
        labels = [item.get_text(" ", strip=True) for item in controls]
        self.assertTrue(any(text.startswith("Request profile") for text in labels))
        self.assertTrue(any(text.startswith("Session mode") for text in labels))
        self.assertNotIn("Avito impersonation", self.html)
        self.assertNotIn("Avito session", self.html)

    def test_source_change_updates_controls_immediately(self) -> None:
        self.assertIn("elements.source.onchange=updateSourceControls", self.script)
        self.assertIn("x.hidden=x.dataset.sourceControl!==source", self.script)
        self.assertNotIn('data-source-control="farpost"', self.html)
        self.assertNotIn('data-source-control="youla"', self.html)


class SearchEditorPersistenceTests(unittest.TestCase):
    def test_partial_simple_save_preserves_advanced_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            config = base / "searches.json"
            original = SearchConfig(
                "Advanced Ryzen", "avito", "", preset_id="cpu_ryzen",
                interval_seconds=2345, jitter_seconds=117,
                block_retry_delay_seconds=82, block_cooldown_seconds=1900,
                max_block_retries=1, include_terms=("ryzen", "am4"),
                exclude_terms=("repair",), brands=("amd",), target_price=7000,
                min_price=1000, max_price=15000, network_route="proxy",
                proxy_url="http://user:secret@127.0.0.1:8080",
                avito_impersonation="edge", avito_session_mode="fresh",
            )
            save_searches(config, [original])
            service = GuiService(config_path=config, database_path=base / "db.sqlite",
                                 output_dir=base / "out")
            service.update_search("Advanced Ryzen", {"min_price": 2000, "enabled": False})
            saved = load_searches(config)[0]
            self.assertEqual(saved.min_price, 2000)
            self.assertFalse(saved.enabled)
            for field in (
                "interval_seconds", "jitter_seconds", "block_retry_delay_seconds",
                "block_cooldown_seconds", "max_block_retries", "include_terms",
                "exclude_terms", "brands", "target_price", "network_route",
                "proxy_url", "avito_impersonation", "avito_session_mode",
            ):
                self.assertEqual(getattr(saved, field), getattr(original, field), field)
            service.close()
