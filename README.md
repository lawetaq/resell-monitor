# Resell Monitor

[![Version](https://img.shields.io/badge/version-0.1.0--alpha-orange)](CHANGELOG.md)
[![Platform](https://img.shields.io/badge/platform-Linux%20x86__64-blue)](#installation)
[![Python](https://img.shields.io/badge/Python-3.13-blue)](#development)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

**Alpha** — a local-first desktop marketplace monitor for finding and evaluating
potentially underpriced PC components.

Resell Monitor brings saved searches, normalized listings, price history, and
deterministic deal analysis into one desktop interface. It is designed around
Khabarovsk today, keeps mutable application data outside the installation, and
does not use AI for valuation.

No approved product screenshots are committed yet. The repository has a
[media contribution structure](docs/media/README.md) ready for real captures.

## Features

- Saved marketplace searches and conservative sequential scanning.
- Shared listing view for Avito, FarPost, and Youla adapters.
- Duplicate detection, listing history, price history, and explicit lifecycle states.
- Rule-based ranking with comparable-market, condition, confidence, liquidity, and risk signals.
- Listing thumbnails, inbox cleanup, sorting, filters, and market analytics.
- TXT, JSON, and HTML exports.
- Russian and English interfaces and multiple themes.
- Native Linux desktop window using pywebview and Qt.
- Local SQLite persistence with verified backups before schema migrations.
- Experimental, manual Browser-Assisted Retail evidence.

## Installation

The first packaged release targets Linux x86_64. Neither method requires Python
on the end user's machine.

### Portable AppImage

Download the release bundle, verify its checksum, then run:

```bash
sha256sum -c ResellMonitor-0.1.0-x86_64.AppImage.sha256
chmod +x ResellMonitor-0.1.0-x86_64.AppImage
./ResellMonitor-0.1.0-x86_64.AppImage
```

### User installation

The release bundle includes the helper and branding assets needed to add Resell
Monitor to KDE, GNOME, and other freedesktop-compatible application menus:

```bash
./install_linux_user.sh ./ResellMonitor-0.1.0-x86_64.AppImage
```

No root access is required. This installs the executable at
`~/.local/bin/resell-monitor`, a desktop entry under
`~/.local/share/applications/`, and icons under the user hicolor icon theme.

To remove the executable and desktop integration while preserving all data:

```bash
./uninstall_linux_user.sh
```

## Quick start

Launching the application does not start a marketplace scan. Add or review a
saved search, then use **Scan now** or **Start monitoring** explicitly. Keep the
number of searches and request frequency conservative.

## Supported sources

| Source | Current status |
| --- | --- |
| Avito | Primary source; HTTP and embedded frontend data first, Playwright fallback when needed |
| FarPost | Available with more limited capabilities than Avito |
| Youla | Experimental and may be degraded when embedded product data is unavailable |

DNS, Ozon, and Wildberries integrations provide experimental retail comparison
evidence. They are not equivalent marketplace sources and can be blocked by
provider behavior.

## How it works

Marketplace adapters normalize raw responses into one shared listing model.
Resell Monitor validates and stores observations locally, tracks availability
and price history, then applies transparent deterministic rules and comparable
market evidence. Marketplace-specific raw dictionaries remain inside adapters.

Access is intentionally conservative. The project does not bypass CAPTCHAs,
rotate proxies automatically, or use stealth/anti-detection libraries.

## Data and privacy

Resell Monitor is currently local-first. Searches, settings, listing history,
analytics, exports, and browser-assisted profiles are stored in the user's
environment. Marketplace and configured retail access originates from that
environment according to the selected source and route.

Typical packaged Linux paths are:

- Database, settings, exports, and backups: `~/.local/share/resell-monitor/`
- Saved-search configuration: `~/.config/resell-monitor/`
- Cache: `~/.cache/resell-monitor/`
- Logs and diagnostics: `~/.local/state/resell-monitor/log/`

The application respects XDG overrides, so expanded paths can differ. Replacing
or uninstalling the AppImage does not remove history, searches, or settings.
Source/development mode retains repository-local `data/`, `searches.json`,
`output/`, and `debug/` defaults.

## Updating

There is no auto-update. Download the newer release bundle and run its installer
with the newer AppImage. The helper atomically replaces the stable executable
path and refreshes integration assets; the separate data/configuration paths are
untouched. Portable users can replace their AppImage file directly.

Settings → About provides a manual availability check against this project's
GitHub Releases. It contacts GitHub only when requested and can open the release
page; it never downloads or installs an update automatically.

## Development

Requirements: Python 3.13 and a supported Linux development environment.

```bash
python3.13 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements-desktop.txt
cp searches.example.json searches.json
.venv/bin/python -m src.desktop --gui qt
```

The example searches are disabled. Browser development mode remains available:

```bash
.venv/bin/python -m src.gui.app --config searches.json
```

GTK is an optional development backend when its distribution packages and
pywebview requirements are installed:

```bash
python -m src.desktop --gui gtk
```

Browser-Assisted Retail and the Avito Playwright fallback require a separately
installed browser runtime. Firefox is the default browser-assisted retail engine:

```bash
.venv/bin/python -m playwright install firefox
```

Run the offline suite without performing marketplace scans:

```bash
.venv/bin/python -m unittest discover -s tests -p 'test_*.py' -v
```

## Building

Install build dependencies and build the accepted PyInstaller ONEDIR package:

```bash
.venv/bin/python -m pip install -r requirements-build.txt
PYTHON=.venv/bin/python scripts/build_linux.sh
```

Build an AppImage with an externally obtained `appimagetool` (the scripts never
download it):

```bash
APPIMAGETOOL=/absolute/path/to/appimagetool-x86_64.AppImage \
PYTHON=.venv/bin/python scripts/build_appimage.sh
```

For a smoke test outside the checkout, resolve the artifact before changing
directories:

```bash
artifact="$(pwd)/dist/ResellMonitor-0.1.0-x86_64.AppImage"
cp "$artifact" /tmp/ResellMonitor-0.1.0-x86_64.AppImage
cd /tmp
./ResellMonitor-0.1.0-x86_64.AppImage
```

Build the complete release bundle by composing that existing AppImage build:

```bash
APPIMAGETOOL=/absolute/path/to/appimagetool-x86_64.AppImage \
PYTHON=.venv/bin/python scripts/build_release_linux.sh
```

Outputs are written to `dist/release/`, including the AppImage, checksum,
release notes, user installation helpers, desktop metadata, and icons. See the
[release checklist](docs/RELEASING.md).

## Known limitations

- This is alpha software; the AppImage currently targets Linux x86_64 and has limited distribution coverage.
- Marketplace and retail endpoints can change or block access, reducing source availability.
- Source feature parity differs; Avito currently has stronger image support than FarPost and Youla.
- Browser-Assisted Retail remains experimental and requires deliberate user interaction.
- Qt may print harmless Vulkan probing or WebEngine teardown warnings on some systems even when launch and shutdown succeed.
- Windows packaging and auto-update are not implemented.

### Linux desktop diagnostics

Qt WebEngine can print `VK_ERROR_INCOMPATIBLE_DRIVER` while probing graphics
backends; if the window renders normally, this is generally a non-fatal fallback.
Some pywebview/Qt combinations also print
`Release of profile requested but WebEnginePage still not deleted` during
teardown. After closing, verify that no process or listener remains:

```bash
pgrep -af 'ResellMonitor|QtWebEngineProcess'
ss -ltnp | grep ResellMonitor
```

## Roadmap

- Windows distribution after the Linux release path is validated.
- Improved notifications and richer image/gallery support.
- Marketplace source quality and parity improvements.
- Security hardening and broader Linux compatibility validation.
- Release automation after the manual release pipeline is proven.

No dates are promised; priorities follow tested product needs.

## License

Resell Monitor is available under the [MIT License](LICENSE).
