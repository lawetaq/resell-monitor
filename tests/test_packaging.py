from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import stat
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from src.desktop import configure_packaged_logging, resolve_desktop_paths
from src.resources import resource_path
from src.storage.sqlite import ListingRepository
from src.user_paths import UserPaths, prepare_installed_user_data
from src.version import __version__
from scripts.assemble_appdir import assemble_appdir


def _args(**overrides: object) -> argparse.Namespace:
    values: dict[str, object] = {
        "config": None, "database": None, "output_dir": None,
        "debug_dir": None, "legacy_root": None,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


class UserPathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.paths = UserPaths(
            self.root / "data", self.root / "config",
            self.root / "cache", self.root / "state" / "log",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_first_run_creates_persistent_directories_and_empty_config(self) -> None:
        result = prepare_installed_user_data(self.paths)
        self.assertFalse(result.database_imported)
        self.assertFalse(result.searches_imported)
        self.assertEqual(json.loads(self.paths.searches_path.read_text()), [])
        for directory in (
            self.paths.data_dir, self.paths.config_dir, self.paths.cache_dir,
            self.paths.log_dir, self.paths.backup_dir, self.paths.export_dir,
        ):
            self.assertTrue(directory.is_dir())

    def test_installed_paths_do_not_depend_on_working_directory(self) -> None:
        with patch("src.user_paths._sqlite_copy") as copy_database:
            first = resolve_desktop_paths(_args(), packaged=True, user_paths=self.paths)
            with patch.object(Path, "cwd", return_value=Path("/unrelated")):
                second = resolve_desktop_paths(_args(), packaged=True, user_paths=self.paths)
        self.assertEqual(first, second)
        self.assertEqual(first.database, self.paths.database_path)
        self.assertEqual(first.config, self.paths.searches_path)
        copy_database.assert_not_called()

    def test_source_mode_keeps_project_local_defaults_and_explicit_paths(self) -> None:
        defaults = resolve_desktop_paths(_args(), packaged=False)
        self.assertEqual(defaults.database, Path("data/resell-monitor.db"))
        self.assertEqual(defaults.config, Path("searches.json"))
        explicit = resolve_desktop_paths(
            _args(config=Path("a.json"), database=Path("a.db"),
                  output_dir=Path("exports")), packaged=True,
        )
        self.assertEqual(explicit.database, Path("a.db"))

    def test_packaged_log_is_outside_bundle(self) -> None:
        with patch("logging.basicConfig") as configure:
            log_path = configure_packaged_logging(self.paths)
        self.assertEqual(log_path, self.paths.log_dir / "resell-monitor.log")
        self.assertEqual(configure.call_args.kwargs["filename"], log_path)

    def test_legacy_database_and_config_are_copied_once_without_mutating_originals(self) -> None:
        legacy = self.root / "legacy"
        legacy_database = legacy / "data" / "resell-monitor.db"
        legacy_database.parent.mkdir(parents=True)
        with sqlite3.connect(legacy_database) as connection:
            connection.execute("CREATE TABLE marker(value TEXT)")
            connection.execute("INSERT INTO marker VALUES ('legacy')")
        legacy_config = legacy / "searches.json"
        legacy_config.write_text('[{"name": "legacy"}]\n', encoding="utf-8")
        before_database = legacy_database.read_bytes()
        before_config = legacy_config.read_bytes()

        result = prepare_installed_user_data(self.paths, legacy_root=legacy)
        self.assertTrue(result.database_imported)
        self.assertTrue(result.searches_imported)
        with sqlite3.connect(self.paths.database_path) as connection:
            self.assertEqual(connection.execute("SELECT value FROM marker").fetchone()[0], "legacy")
        self.assertEqual(legacy_database.read_bytes(), before_database)
        self.assertEqual(legacy_config.read_bytes(), before_config)

        self.paths.searches_path.write_text("[]\n", encoding="utf-8")
        second = prepare_installed_user_data(self.paths, legacy_root=legacy)
        self.assertFalse(second.database_imported)
        self.assertFalse(second.searches_imported)
        self.assertEqual(self.paths.searches_path.read_text(), "[]\n")

    def test_copy_failure_leaves_legacy_untouched_and_no_destination(self) -> None:
        legacy = self.root / "legacy"
        legacy_database = legacy / "data" / "resell-monitor.db"
        legacy_database.parent.mkdir(parents=True)
        legacy_database.write_bytes(b"not sqlite")
        before = legacy_database.read_bytes()
        with self.assertRaises(sqlite3.DatabaseError):
            prepare_installed_user_data(self.paths, legacy_root=legacy)
        self.assertEqual(legacy_database.read_bytes(), before)
        self.assertFalse(self.paths.database_path.exists())


class SchemaBackupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.database = self.root / "monitor.db"
        with ListingRepository(self.database) as repository:
            repository.connection.execute(
                "INSERT INTO app_settings(key, value) VALUES ('marker', '\"kept\"')"
            )
            repository.connection.commit()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _make_v9(self) -> None:
        with sqlite3.connect(self.database) as connection:
            connection.execute("ALTER TABLE listings DROP COLUMN primary_image_url")
            connection.execute("PRAGMA user_version = 9")

    def test_current_schema_open_is_write_free_and_creates_no_backup(self) -> None:
        before = self.database.stat().st_mtime_ns
        with ListingRepository(self.database):
            pass
        self.assertEqual(self.database.stat().st_mtime_ns, before)
        self.assertFalse((self.root / "backups").exists())

    def test_old_schema_is_backed_up_before_successful_migration(self) -> None:
        self._make_v9()
        with ListingRepository(self.database) as repository:
            self.assertEqual(repository.connection.execute("PRAGMA user_version").fetchone()[0], 10)
        backups = list((self.root / "backups").glob("monitor-schema-v9-*.db"))
        self.assertEqual(len(backups), 1)
        with sqlite3.connect(backups[0]) as backup:
            self.assertEqual(backup.execute("PRAGMA user_version").fetchone()[0], 9)
            self.assertEqual(backup.execute("SELECT value FROM app_settings WHERE key='marker'").fetchone()[0], '"kept"')

    def test_backup_failure_prevents_migration(self) -> None:
        self._make_v9()
        with patch.object(ListingRepository, "_create_migration_backup", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                ListingRepository(self.database)
        with sqlite3.connect(self.database) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 9)

    def test_migration_failure_keeps_backup_and_original_schema(self) -> None:
        self._make_v9()
        with patch.object(ListingRepository, "_migrate_listing_images_v10", side_effect=RuntimeError("migration failed")):
            with self.assertRaisesRegex(RuntimeError, "migration failed"):
                ListingRepository(self.database)
        self.assertEqual(len(list((self.root / "backups").glob("monitor-schema-v9-*.db"))), 1)
        with sqlite3.connect(self.database) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 9)

    def test_newer_schema_fails_without_rewrite_or_empty_database(self) -> None:
        with sqlite3.connect(self.database) as connection:
            connection.execute("PRAGMA user_version = 99")
        with self.assertRaisesRegex(RuntimeError, "newer than supported"):
            ListingRepository(self.database)
        with sqlite3.connect(self.database) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 99)


class PackagingContractTests(unittest.TestCase):
    def test_read_only_resources_are_resolved_from_central_helper(self) -> None:
        self.assertTrue(resource_path("src/gui/static/index.html").is_file())
        self.assertTrue(resource_path("src/location_registry.json").is_file())
        with self.assertRaises(ValueError):
            resource_path("../searches.json")

    def test_frozen_resources_resolve_from_pyinstaller_bundle_root(self) -> None:
        bundle = Path("/tmp/resell-monitor-test-bundle")
        with patch("src.resources.sys.frozen", True, create=True), \
             patch("src.resources.sys._MEIPASS", str(bundle), create=True):
            self.assertEqual(resource_path("src/gui/static/app.js"), bundle / "src/gui/static/app.js")

    def test_spec_bundles_resources_and_qt_but_not_mutable_user_state(self) -> None:
        root = Path(__file__).parents[1]
        spec_path = root / "packaging/ResellMonitor.spec"
        spec = spec_path.read_text()
        path_setup = spec.split("analysis = Analysis(", 1)[0]
        namespace: dict[str, object] = {"SPECPATH": str(spec_path.parent)}
        exec(compile(path_setup, spec_path, "exec"), namespace)
        resolved_root = namespace["project_root"]
        datas = namespace["datas"]

        self.assertEqual(resolved_root, root.resolve())
        entry_point = resolved_root / "src" / "desktop.py"
        self.assertEqual(entry_point, root.resolve() / "src" / "desktop.py")
        self.assertTrue(entry_point.is_file())
        for source, _destination in datas:
            source_path = Path(source)
            self.assertTrue(source_path.exists())
            self.assertTrue(source_path.is_relative_to(root.resolve()))
            self.assertNotEqual(source_path.parent, root.resolve().parent)
        self.assertIn('"src" / "gui" / "static"', spec)
        self.assertIn('"src" / "location_registry.json"', spec)
        self.assertIn('"assets" / "branding"', spec)
        self.assertIn('"webview.platforms.qt"', spec)
        self.assertNotIn("/home/", spec)
        self.assertNotIn("searches.json", spec)
        self.assertNotIn("resell-monitor.db", spec)
        build_script = (root / "scripts/build_linux.sh").read_text()
        self.assertIn('project_root=$(CDPATH= cd -- "$script_dir/.." && pwd)', build_script)
        self.assertIn('"$project_root/dist"', build_script)
        self.assertNotIn("/home/", build_script)
        build_requirements = (root / "requirements-build.txt").read_text()
        self.assertIn("pyinstaller>=6,<7", build_requirements)
        self.assertIn("-r requirements-desktop.txt", build_requirements)

    def test_branding_contract_uses_one_accessible_token_driven_mark(self) -> None:
        root = Path(__file__).parents[1]
        index = (root / "src/gui/static/index.html").read_text()
        css = (root / "src/gui/static/styles.css").read_text()
        brand = index.split('<div class="brand">', 1)[1].split("</div></div>", 1)[0]
        self.assertNotIn('<span class="brand-mark">R</span>', index)
        self.assertIn('<svg viewBox="0 0 30 30"', brand)
        self.assertIn('aria-hidden="true"', brand)
        self.assertIn('class="brand-line"', brand)
        self.assertIn('class="brand-dot"', brand)
        for token in ("--brand-surface", "--brand-border", "--brand-grid", "--brand-line"):
            self.assertIn(token, css)
        self.assertIn("[data-mode=\"dark\"]", css)
        self.assertIn("[data-mode=\"light\"]", css)
        self.assertNotIn("#77a0a0", css[css.index("/* Theme-aware brand geometry"):])
        for size in (32, 48, 64, 128, 256, 512):
            self.assertTrue((root / f"assets/branding/resell-monitor-{size}.png").is_file())
        self.assertTrue((root / "assets/branding/resell-monitor.svg").is_file())

    def test_appimage_contract_wraps_onedir_without_mutable_state(self) -> None:
        root = Path(__file__).parents[1]
        app_run = root / "packaging/AppRun"
        desktop_entry = (root / "packaging/resell-monitor.desktop").read_text()
        build_script = (root / "scripts/build_appimage.sh").read_text()
        self.assertTrue(app_run.stat().st_mode & stat.S_IXUSR)
        self.assertIn('appdir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)', app_run.read_text())
        self.assertIn('exec "$executable" "$@"', app_run.read_text())
        self.assertIn("Icon=resell-monitor", desktop_entry)
        self.assertIn("Exec=ResellMonitor", desktop_entry)
        self.assertIn("from src.version import __version__", build_script)
        self.assertIn("ResellMonitor-${version}-${architecture}.AppImage", build_script)
        self.assertIn("APPIMAGETOOL", build_script)
        self.assertNotIn("searches.json", app_run.read_text() + build_script)
        self.assertNotIn("resell-monitor.db", app_run.read_text() + build_script)

    def test_appimage_acceptance_copy_is_safe_across_directory_change(self) -> None:
        readme = (Path(__file__).parents[1] / "README.md").read_text()
        self.assertIn('artifact="$(pwd)/dist/ResellMonitor-0.1.0-x86_64.AppImage"', readme)
        self.assertIn('cp "$artifact" /tmp/ResellMonitor-0.1.0-x86_64.AppImage', readme)
        self.assertLess(readme.index('cp "$artifact"'), readme.index("cd /tmp"))
        self.assertIn("VK_ERROR_INCOMPATIBLE_DRIVER", readme)
        self.assertIn("WebEnginePage still not deleted", readme)
        self.assertIn("pgrep -af 'ResellMonitor|QtWebEngineProcess'", readme)

    def test_appdir_assembly_is_cwd_independent_and_contains_no_user_data(self) -> None:
        source_root = Path(__file__).parents[1]
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            bundle = root / "dist/ResellMonitor"
            bundle.mkdir(parents=True)
            executable = bundle / "ResellMonitor"
            executable.write_bytes(b"fixture")
            packaging = root / "packaging"
            packaging.mkdir()
            (packaging / "AppRun").write_bytes((source_root / "packaging/AppRun").read_bytes())
            (packaging / "resell-monitor.desktop").write_bytes(
                (source_root / "packaging/resell-monitor.desktop").read_bytes()
            )
            (packaging / "resell-monitor.metainfo.xml").write_bytes(
                (source_root / "packaging/resell-monitor.metainfo.xml").read_bytes()
            )
            branding = root / "assets/branding"
            branding.mkdir(parents=True)
            (branding / "resell-monitor-256.png").write_bytes(
                (source_root / "assets/branding/resell-monitor-256.png").read_bytes()
            )
            appdir = root / "build/appimage/ResellMonitor.AppDir"
            assemble_appdir(bundle, appdir, root)
            self.assertTrue((appdir / "AppRun").stat().st_mode & stat.S_IXUSR)
            self.assertTrue((appdir / "usr/lib/resell-monitor/ResellMonitor").is_file())
            self.assertTrue((appdir / "usr/bin/ResellMonitor").is_symlink())
            self.assertTrue((appdir / "resell-monitor.desktop").is_file())
            self.assertTrue((appdir / "resell-monitor.png").is_file())
            self.assertTrue(
                (appdir / "usr/share/metainfo/resell-monitor.metainfo.xml").is_file()
            )
            names = {path.name for path in appdir.rglob("*")}
            self.assertNotIn("searches.json", names)
            self.assertNotIn("resell-monitor.db", names)

    def test_version_is_canonical_and_desktop_entry_is_non_installing_template(self) -> None:
        root = Path(__file__).parents[1]
        index = (root / "src/gui/static/index.html").read_text()
        service = (root / "src/gui/service.py").read_text()
        desktop_entry = (root / "packaging/resell-monitor.desktop").read_text()
        self.assertRegex(__version__, r"^\d+\.\d+\.\d+$")
        self.assertIn("api('/api/project')", (root / "src/gui/static/app.js").read_text())
        self.assertIn("version", index)
        self.assertIn("from src.version import __version__", service)
        self.assertIn("Exec=ResellMonitor", desktop_entry)
        self.assertNotIn("~/.local/share/applications", desktop_entry)


if __name__ == "__main__":
    unittest.main()
