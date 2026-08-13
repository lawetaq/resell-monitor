from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.config import SEARCH_PRESETS, load_searches, parse_searches, save_searches
from src.gui.service import GuiService
from src.locations import (DEFAULT_LOCATION, KNOWN_LOCATIONS, build_search_url,
                           detect_location_from_url, learn_location_from_url,
                           profile_to_json, search_locations, source_resolution,
                           validate_marketplace_url)
from src.models import LocationMode, SearchConfig


class LocationArchitectureTests(unittest.TestCase):
    def test_common_city_registry_search_aliases_and_avito_urls(self) -> None:
        expected = {
            "Москва": "moskva",
            "Санкт-Петербург": "sankt-peterburg",
            "Новосибирск": "novosibirsk",
            "Екатеринбург": "ekaterinburg",
        }
        for display_name, token in expected.items():
            matches = search_locations(display_name.swapcase())
            self.assertEqual(matches[0].display_name, display_name)
            self.assertEqual(matches[0].source_tokens["avito"], token)
            search = SearchConfig("Ryzen", "avito", "", preset_id="cpu_ryzen")
            self.assertIn(f"/{token}/", build_search_url(search, matches[0]))
        self.assertEqual(search_locations("мск")[0].id, "moscow")
        self.assertEqual(search_locations("SPB")[0].id, "saint-petersburg")
        self.assertEqual(search_locations("спб")[0].id, "saint-petersburg")
        self.assertGreaterEqual(len(KNOWN_LOCATIONS), 25)

    def test_unsupported_source_mapping_is_explicit_and_never_guessed(self) -> None:
        moscow = KNOWN_LOCATIONS["moscow"]
        self.assertEqual(source_resolution(moscow), {
            "avito": "ready", "farpost": "not-configured", "youla": "unsupported",
        })
        self.assertNotIn("farpost", moscow.source_tokens)
    def test_source_specific_building_and_modes(self) -> None:
        base = SearchConfig("Ryzen", "avito", "", preset_id="cpu_ryzen")
        self.assertEqual(build_search_url(base, DEFAULT_LOCATION),
                         "https://www.avito.ru/habarovsk/tovary_dlya_kompyutera/komplektuyuschie?q=ryzen")
        specific = SearchConfig("Ryzen", "avito", "", preset_id="cpu_ryzen",
                                location_mode=LocationMode.SPECIFIC,
                                specific_location=KNOWN_LOCATIONS["vladivostok"])
        self.assertIn("/vladivostok/", build_search_url(specific, DEFAULT_LOCATION))
        nationwide = SearchConfig("Ryzen", "avito", "", preset_id="cpu_ryzen",
                                  location_mode=LocationMode.ALL)
        self.assertIn("/rossiya/", build_search_url(nationwide, DEFAULT_LOCATION))
        farpost = SearchConfig("GPU", "farpost", "", preset_id="farpost_gpu",
                               location_mode=LocationMode.ALL)
        self.assertEqual(build_search_url(farpost, DEFAULT_LOCATION),
                         "https://www.farpost.ru/tech/computers/components/video/?query=%D0%B2%D0%B8%D0%B4%D0%B5%D0%BE%D0%BA%D0%B0%D1%80%D1%82%D0%B0")

    def test_default_changes_at_runtime_but_specific_does_not(self) -> None:
        search = SearchConfig("RAM", "avito", "", preset_id="ram")
        self.assertIn("/moskva/", build_search_url(search, KNOWN_LOCATIONS["moscow"]))
        self.assertIn("/novosibirsk/", build_search_url(search, KNOWN_LOCATIONS["novosibirsk"]))
        fixed = SearchConfig("RAM", "avito", "", preset_id="ram",
                             location_mode=LocationMode.SPECIFIC,
                             specific_location=KNOWN_LOCATIONS["ekaterinburg"])
        self.assertEqual(build_search_url(fixed, KNOWN_LOCATIONS["moscow"]),
                         build_search_url(fixed, KNOWN_LOCATIONS["novosibirsk"]))
        self.assertIn("/ekaterinburg/", build_search_url(fixed, DEFAULT_LOCATION))

    def test_presets_have_no_habarovsk_and_old_searches_migrate_in_memory(self) -> None:
        self.assertNotIn("habarovsk", json.dumps(SEARCH_PRESETS).casefold())
        old = parse_searches([{"name": "old", "source": "avito",
                               "url": "https://www.avito.ru/habarovsk/components"}])[0]
        self.assertEqual(old.location_mode, LocationMode.DEFAULT)
        self.assertEqual(old.url, "https://www.avito.ru/habarovsk/components")

    def test_custom_url_detection_validation_and_learning_are_local(self) -> None:
        with patch("urllib.request.urlopen", side_effect=AssertionError("network called")):
            source, profile = detect_location_from_url("https://www.avito.ru/vladivostok/components")
        self.assertEqual((source, profile.display_name if profile else None), ("avito", "Владивосток"))
        for bad in ("http://www.avito.ru/x", "file:///tmp/x", "https://localhost/x",
                    "https://avito.ru.evil.test/habarovsk/x"):
            with self.assertRaises(ValueError):
                validate_marketplace_url(bad)
        learned = learn_location_from_url("Кавалерово", "https://www.farpost.ru/kavalerovo/tech",
                                          source="farpost", location_id="kavalerovo")
        self.assertEqual(learned.source_tokens, {"farpost": "kavalerovo"})

    def test_custom_learning_persists_and_does_not_guess_other_sources(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            service = GuiService(config_path=base / "searches.json",
                                 database_path=base / "db.sqlite", output_dir=base / "out")
            learned = service.learn_location(display_name="Кавалерово", location_id="custom-kavalerovo",
                                             source="farpost",
                                             url="https://www.farpost.ru/kavalerovo/tech")
            self.assertEqual(learned["source_status"]["farpost"], "ready")
            self.assertEqual(learned["source_status"]["avito"], "not-configured")
            service.close()
            reopened = GuiService(config_path=base / "searches.json",
                                  database_path=base / "db.sqlite", output_dir=base / "out")
            match = reopened.locations("кавалерово")[0]
            self.assertEqual(match["source_tokens"], {"farpost": "kavalerovo"})
            reopened.close()

    def test_settings_persistence_runtime_resolution_diagnostics_and_shared_model(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            config = base / "searches.json"
            save_searches(config, [SearchConfig("RAM", "avito", "", preset_id="ram")])
            calls: list[str] = []
            def runner(searches, progress):
                calls.append(searches[0].url)
                return []
            service = GuiService(config_path=config, database_path=base / "db.sqlite",
                                 output_dir=base / "out", scan_runner=runner)
            service.update_settings({"default_location": profile_to_json(KNOWN_LOCATIONS["vladivostok"]),
                                     "default_search_editor": "advanced"})
            service.close()
            service = GuiService(config_path=config, database_path=base / "db.sqlite",
                                 output_dir=base / "out", scan_runner=runner)
            self.assertEqual(service.default_location().id, "vladivostok")
            self.assertEqual(service.settings()["default_search_editor"], "advanced")
            service.scan_now()
            self.assertIn("/vladivostok/", calls[-1])
            self.assertIn("Database schema: 9", service.diagnostic_report())
            simple_payload = {"name":"x", "source":"avito", "url":"", "preset_id":"ram",
                              "location_mode":"all", "interval_seconds":900}
            created = service.create_search(simple_payload)
            self.assertEqual(load_searches(config)[-1].location_mode, LocationMode.ALL)
            self.assertEqual(created["preset_id"], "ram")
            service.close()

    def test_builtin_default_persists_as_stable_id(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            service = GuiService(config_path=base / "searches.json",
                                 database_path=base / "db.sqlite", output_dir=base / "out")
            service.update_settings({"default_location": "moscow"})
            self.assertEqual(service.settings()["default_location"], "moscow")
            self.assertEqual(service.default_location().display_name, "Москва")
            service.close()

    def test_settings_navigation_sections_exist(self) -> None:
        html = (Path(__file__).parents[1] / "src/gui/static/index.html").read_text()
        for section in ("general", "search", "sources", "appearance", "language",
                        "data", "privacy", "support", "about"):
            self.assertIn(f'data-settings-section="{section}"', html)
        self.assertIn('list="location-options"', html)
        script = (Path(__file__).parents[1] / "src/gui/static/app.js").read_text()
        self.assertIn("api('/api/locations')", script)
        self.assertIn("t('search.default')", script)
