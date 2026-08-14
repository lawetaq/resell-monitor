from pathlib import Path


# PyInstaller defines SPECPATH as the directory containing this spec file.
# packaging/ is directly inside the repository, so its parent is the root.
spec_dir = Path(SPECPATH).resolve()
project_root = spec_dir.parent
datas = [
    (str(project_root / "src" / "gui" / "static"), "src/gui/static"),
    (str(project_root / "src" / "location_registry.json"), "src"),
    (str(project_root / "assets" / "branding"), "assets/branding"),
    (str(project_root / "packaging" / "resell-monitor.desktop"), "packaging"),
]

analysis = Analysis(
    [str(project_root / "src" / "desktop.py")],
    pathex=[str(project_root)],
    binaries=[],
    datas=datas,
    hiddenimports=["webview.platforms.qt"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["gi", "webview.platforms.gtk"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(analysis.pure)

executable = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="ResellMonitor",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
)

bundle = COLLECT(
    executable,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="ResellMonitor",
)
