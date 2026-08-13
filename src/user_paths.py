from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import sqlite3


APP_NAME = "resell-monitor"
DATABASE_NAME = "resell-monitor.db"
SEARCH_CONFIG_NAME = "searches.json"


@dataclass(slots=True, frozen=True)
class UserPaths:
    data_dir: Path
    config_dir: Path
    cache_dir: Path
    log_dir: Path

    @classmethod
    def from_platform(cls) -> UserPaths:
        try:
            from platformdirs import PlatformDirs
        except ImportError as error:
            raise RuntimeError(
                "Installed mode requires platformdirs; install desktop dependencies."
            ) from error
        directories = PlatformDirs(APP_NAME, appauthor=False, roaming=False)
        return cls(
            Path(directories.user_data_dir),
            Path(directories.user_config_dir),
            Path(directories.user_cache_dir),
            Path(directories.user_log_dir),
        )

    @property
    def database_path(self) -> Path:
        return self.data_dir / DATABASE_NAME

    @property
    def searches_path(self) -> Path:
        return self.config_dir / SEARCH_CONFIG_NAME

    @property
    def backup_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def export_dir(self) -> Path:
        return self.data_dir / "exports"

    @property
    def debug_dir(self) -> Path:
        return self.log_dir / "source-debug"

    def create_directories(self) -> None:
        for path in (
            self.data_dir,
            self.config_dir,
            self.cache_dir,
            self.log_dir,
            self.backup_dir,
            self.export_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)


@dataclass(slots=True, frozen=True)
class LegacyImportResult:
    database_imported: bool = False
    searches_imported: bool = False


def prepare_installed_user_data(
    paths: UserPaths, *, legacy_root: Path | None = None
) -> LegacyImportResult:
    """Create installed-mode state and conservatively copy known legacy files."""

    paths.create_directories()
    database_imported = False
    searches_imported = False
    if legacy_root is not None:
        root = legacy_root.resolve()
        legacy_database = root / "data" / DATABASE_NAME
        legacy_searches = root / SEARCH_CONFIG_NAME
        if not paths.database_path.exists() and legacy_database.is_file():
            _sqlite_copy(legacy_database, paths.database_path)
            database_imported = True
        if not paths.searches_path.exists() and legacy_searches.is_file():
            _config_copy(legacy_searches, paths.searches_path)
            searches_imported = True
    if not paths.searches_path.exists():
        paths.searches_path.write_text("[]\n", encoding="utf-8")
    return LegacyImportResult(database_imported, searches_imported)


def _sqlite_copy(source: Path, destination: Path) -> None:
    if destination.exists():
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_connection: sqlite3.Connection | None = None
    destination_connection: sqlite3.Connection | None = None
    try:
        source_uri = f"file:{source.resolve().as_posix()}?mode=ro"
        source_connection = sqlite3.connect(source_uri, uri=True)
        destination_connection = sqlite3.connect(destination)
        source_connection.backup(destination_connection)
        check = destination_connection.execute("PRAGMA quick_check").fetchone()
        if check is None or check[0] != "ok":
            raise RuntimeError("legacy database copy failed SQLite verification")
    except BaseException:
        if destination_connection is not None:
            destination_connection.close()
            destination_connection = None
        if destination.exists():
            destination.unlink()
        raise
    finally:
        if destination_connection is not None:
            destination_connection.close()
        if source_connection is not None:
            source_connection.close()


def _config_copy(source: Path, destination: Path) -> None:
    raw = source.read_text(encoding="utf-8")
    parsed = json.loads(raw)
    if not isinstance(parsed, list):
        raise ValueError("legacy searches configuration must be a JSON array")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".importing")
    if temporary.exists():
        temporary.unlink()
    try:
        temporary.write_text(raw, encoding="utf-8")
        os.replace(temporary, destination)
    except BaseException:
        if temporary.exists():
            temporary.unlink()
        raise
