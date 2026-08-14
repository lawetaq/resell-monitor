from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from typing import Mapping, Sequence

from src.resources import is_frozen, resource_path


APPLICATION_ID = "resell-monitor"
ICON_SIZES = (32, 48, 64, 128, 256, 512)
EXECUTABLE_NAME = "resell-monitor"
DESKTOP_FILENAME = "resell-monitor.desktop"


class IntegrationStatus(StrEnum):
    UNAVAILABLE = "unavailable"
    PORTABLE = "portable"
    INTEGRATED = "integrated"


@dataclass(slots=True, frozen=True)
class IntegrationPaths:
    executable: Path
    desktop_entry: Path
    icons_root: Path

    @classmethod
    def from_environment(cls, environment: Mapping[str, str]) -> IntegrationPaths:
        home = _absolute_root(environment.get("HOME") or str(Path.home()), "HOME")
        data_home = _absolute_root(
            environment.get("XDG_DATA_HOME") or str(home / ".local/share"),
            "XDG_DATA_HOME",
        )
        bin_home = _absolute_root(
            environment.get("XDG_BIN_HOME") or str(home / ".local/bin"),
            "XDG_BIN_HOME",
        )
        return cls(
            executable=bin_home / EXECUTABLE_NAME,
            desktop_entry=data_home / "applications" / DESKTOP_FILENAME,
            icons_root=data_home / "icons/hicolor",
        )

    def icon(self, size: int) -> Path:
        return self.icons_root / f"{size}x{size}/apps/{APPLICATION_ID}.png"


class LinuxIntegration:
    """Fixed-purpose user desktop integration for the running AppImage."""

    def __init__(
        self,
        *,
        environment: Mapping[str, str] | None = None,
        platform: str | None = None,
        frozen: bool | None = None,
    ) -> None:
        self._environment = dict(os.environ if environment is None else environment)
        self._platform = sys.platform if platform is None else platform
        self._frozen = is_frozen() if frozen is None else frozen

    def status(self) -> dict[str, object]:
        source = self._appimage_source()
        if source is None:
            return {"status": IntegrationStatus.UNAVAILABLE, "can_install": False}
        try:
            paths = IntegrationPaths.from_environment(self._environment)
        except ValueError:
            return {"status": IntegrationStatus.UNAVAILABLE, "can_install": False}
        status = (
            IntegrationStatus.INTEGRATED
            if self._integration_is_complete(paths)
            else IntegrationStatus.PORTABLE
        )
        return {"status": status, "can_install": True}

    def install(self) -> dict[str, object]:
        source = self._appimage_source()
        if source is None:
            return {
                "status": IntegrationStatus.UNAVAILABLE,
                "can_install": False,
                "result": "unavailable",
            }
        paths = IntegrationPaths.from_environment(self._environment)
        template_path = resource_path("packaging/resell-monitor.desktop")
        icon_sources = {
            size: resource_path(f"assets/branding/resell-monitor-{size}.png")
            for size in ICON_SIZES
        }
        if not template_path.is_file() or any(
            not source.is_file() for source in icon_sources.values()
        ):
            raise FileNotFoundError("bundled desktop integration resources are unavailable")
        _ensure_directory(paths.executable.parent)
        applications_dir = _ensure_directory(paths.desktop_entry.parent)
        icon_directories = {
            size: _ensure_directory(paths.icon(size).parent) for size in ICON_SIZES
        }
        _reject_file_symlink(paths.executable)
        _atomic_copy(source, paths.executable, mode=0o755)

        template = template_path.read_text(encoding="utf-8")
        desktop_text = _render_desktop_entry(template, paths.executable)
        _reject_file_symlink(paths.desktop_entry)
        _atomic_write(desktop_text.encode("utf-8"), paths.desktop_entry, mode=0o644)

        for size, directory in icon_directories.items():
            destination = directory / f"{APPLICATION_ID}.png"
            _reject_file_symlink(destination)
            _atomic_copy(
                icon_sources[size],
                destination,
                mode=0o644,
            )
        _refresh_desktop_caches(applications_dir, paths.icons_root)
        return {
            "status": IntegrationStatus.INTEGRATED,
            "can_install": True,
            "result": "installed",
            "restart_required": False,
        }

    def _appimage_source(self) -> Path | None:
        if self._platform != "linux" or not self._frozen:
            return None
        raw = self._environment.get("APPIMAGE", "")
        if not raw or _has_control_character(raw):
            return None
        source = Path(raw)
        if not source.is_absolute() or source.is_symlink() or not source.is_file():
            return None
        return source

    @staticmethod
    def _integration_is_complete(paths: IntegrationPaths) -> bool:
        targets = [paths.executable, paths.desktop_entry]
        targets.extend(paths.icon(size) for size in ICON_SIZES)
        if any(path.is_symlink() or not path.is_file() for path in targets):
            return False
        try:
            desktop = paths.desktop_entry.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return False
        return (
            (
                f"Exec={_desktop_exec(paths.executable)}" in desktop
                or f"Exec={paths.executable}" in desktop
            )
            and f"Icon={APPLICATION_ID}" in desktop
        )


def _absolute_root(raw: str, name: str) -> Path:
    if not raw or _has_control_character(raw):
        raise ValueError(f"{name} must be a valid absolute path")
    path = Path(raw)
    if not path.is_absolute() or path == Path("/") or ".." in path.parts:
        raise ValueError(f"{name} must be a user-level absolute path")
    return path


def _ensure_directory(path: Path) -> Path:
    for candidate in (path, *path.parents):
        if candidate.is_symlink():
            raise OSError("desktop integration directory is unsafe")
    if path.exists() and (path.is_symlink() or not path.is_dir()):
        raise OSError("desktop integration directory is unavailable")
    path.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise OSError("desktop integration directory is unsafe")
    return path


def _reject_file_symlink(path: Path) -> None:
    if path.is_symlink():
        raise OSError("desktop integration destination is unsafe")
    if path.exists() and not path.is_file():
        raise OSError("desktop integration destination is not a regular file")


def _atomic_copy(source: Path, destination: Path, *, mode: int) -> None:
    descriptor, name = tempfile.mkstemp(
        prefix=f".{destination.name}.installing-", dir=destination.parent
    )
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as writer, source.open("rb") as reader:
            shutil.copyfileobj(reader, writer)
            writer.flush()
            os.fsync(writer.fileno())
        temporary.chmod(mode)
        if temporary.stat().st_size != source.stat().st_size:
            raise OSError("desktop integration copy validation failed")
        _reject_file_symlink(destination)
        os.replace(temporary, destination)
        _fsync_directory(destination.parent)
    finally:
        if temporary.exists():
            temporary.unlink()


def _atomic_write(content: bytes, destination: Path, *, mode: int) -> None:
    descriptor, name = tempfile.mkstemp(
        prefix=f".{destination.name}.installing-", dir=destination.parent
    )
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as writer:
            writer.write(content)
            writer.flush()
            os.fsync(writer.fileno())
        temporary.chmod(mode)
        _reject_file_symlink(destination)
        os.replace(temporary, destination)
        _fsync_directory(destination.parent)
    finally:
        if temporary.exists():
            temporary.unlink()

def _fsync_directory(directory: Path) -> None:
    try:
        descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _render_desktop_entry(template: str, executable: Path) -> str:
    lines = template.splitlines()
    replaced = False
    for index, line in enumerate(lines):
        if line.startswith("Exec="):
            lines[index] = f"Exec={_desktop_exec(executable)}"
            replaced = True
    if not replaced:
        raise ValueError("desktop entry template has no Exec field")
    return "\n".join(lines) + "\n"


def _desktop_exec(path: Path) -> str:
    value = str(path)
    if _has_control_character(value):
        raise ValueError("desktop executable path contains control characters")
    escaped = value.replace("%", "%%").replace("\\", "\\\\")
    for character in ('"', "`", "$"):
        escaped = escaped.replace(character, f"\\{character}")
    return f'"{escaped}"'


def _refresh_desktop_caches(applications_dir: Path, icons_root: Path) -> None:
    commands: Sequence[tuple[str, list[str]]] = (
        ("update-desktop-database", [str(applications_dir)]),
        ("gtk-update-icon-cache", ["-f", "-t", str(icons_root)]),
    )
    for executable, arguments in commands:
        resolved = shutil.which(executable)
        if resolved is None:
            continue
        try:
            subprocess.run(
                [resolved, *arguments],
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue


def _has_control_character(value: str) -> bool:
    return any(ord(character) < 32 or ord(character) == 127 for character in value)
