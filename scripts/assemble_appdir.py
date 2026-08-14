from __future__ import annotations

import argparse
from pathlib import Path
import shutil


def assemble_appdir(bundle: Path, appdir: Path, project_root: Path) -> None:
    """Assemble an AppDir around an existing PyInstaller ONEDIR bundle."""

    root = project_root.resolve()
    source = bundle.resolve()
    destination = appdir.resolve()
    expected_parent = root / "build" / "appimage"
    if destination.parent != expected_parent:
        raise ValueError("AppDir destination must be directly inside build/appimage")
    if source != root / "dist" / "ResellMonitor":
        raise ValueError("bundle must be the repository ONEDIR artifact")
    executable = source / "ResellMonitor"
    if not executable.is_file():
        raise FileNotFoundError(f"ONEDIR executable not found: {executable}")

    if destination.exists():
        shutil.rmtree(destination)
    application_dir = destination / "usr" / "lib" / "resell-monitor"
    application_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, application_dir, symlinks=True)
    shutil.copy2(root / "packaging" / "AppRun", destination / "AppRun")
    shutil.copy2(
        root / "packaging" / "resell-monitor.desktop",
        destination / "resell-monitor.desktop",
    )
    metainfo_dir = destination / "usr" / "share" / "metainfo"
    metainfo_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(
        root / "packaging" / "resell-monitor.metainfo.xml",
        metainfo_dir / "resell-monitor.metainfo.xml",
    )
    icon = root / "assets" / "branding" / "resell-monitor-256.png"
    shutil.copy2(icon, destination / "resell-monitor.png")
    (destination / "AppRun").chmod(0o755)
    bundled_executable = application_dir / "ResellMonitor"
    bundled_executable.chmod(bundled_executable.stat().st_mode | 0o111)
    binary_dir = destination / "usr" / "bin"
    binary_dir.mkdir(parents=True, exist_ok=True)
    (binary_dir / "ResellMonitor").symlink_to("../lib/resell-monitor/ResellMonitor")
    (destination / ".DirIcon").symlink_to("resell-monitor.png")


def main() -> int:
    parser = argparse.ArgumentParser(description="Assemble Resell Monitor AppDir")
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--appdir", type=Path, required=True)
    args = parser.parse_args()
    assemble_appdir(args.bundle, args.appdir, args.project_root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
