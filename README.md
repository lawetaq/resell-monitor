# Resell Monitor

Resell Monitor is a local marketplace monitoring and resale-analytics application
for finding potentially underpriced PC components. The current default region and
example searches target Khabarovsk, but the normalized domain and storage layers
are marketplace-independent.

The application stores observations locally and uses deterministic analytics; it
does not use AI to value listings.

## Current functionality

- Source-adapter architecture for Avito, FarPost, and Youla.
- Conservative, sequential marketplace access with bounded retry/cooldown state.
- SQLite persistence, duplicate detection, listing history, and price history.
- PC-component normalization and comparable-product matching for GPUs, CPUs,
  RAM, and SSDs.
- Explicit `ACTIVE`, `STALE`, `DISAPPEARED`, `ARCHIVED`, and `UNKNOWN`
  availability semantics.
- Condition and risk-phrase detection independent of resale scoring.
- Dynamic resale scoring using local comparable observations and market trends.
- Historical used-price snapshots, quartiles, activity, and turnover signals.
- Retail-intelligence interfaces and experimental DNS, Ozon, and Wildberries
  providers.
- Local web GUI, background monitoring, saved searches, filters, and JSON/TXT/HTML
  exports.
- Offline parser, analytics, persistence, migration, GUI-service, and scheduler
  tests.

Retail live access is experimental. DNS, Ozon, or Wildberries may block or alter
consumer-facing endpoints depending on network, region, and provider behavior.
Retail data is treated as secondary evidence and does not become authoritative
when it is stale, ambiguous, or low-confidence.

## Safety and request policy

Use a small number of broad, first-page searches and filter results locally.
Scans run sequentially at conservative intervals. The application does not
bypass CAPTCHAs, rotate proxies, randomize identities, or use stealth libraries.
It does not request listing-detail pages merely to determine availability.

Avito attempts use ordinary HTTP retrieval first, embedded frontend JSON when
available, and Playwright only as a fallback. HTTP 403/429 responses do not
trigger escalating transport retries; the search records its state and observes
configured retry/cooldown limits.

## Requirements

- Python 3.13
- A supported Linux environment for the current development workflow
- Chromium only if the optional Playwright fallback or diagnostics are used

Runtime Python dependencies are listed in `requirements.txt`.

## Installation

From the repository root:

```bash
python3.13 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
cp searches.example.json searches.json
```

The example searches are disabled. Review their URLs and filters, then enable
only the searches you intend to run. `searches.json` is local configuration and
is intentionally ignored by Git.

For the default Browser-Assisted Retail engine, install Playwright Firefox separately:

```bash
.venv/bin/python -m playwright install firefox
```

Chromium remains available as an explicit alternative via
`.venv/bin/python -m playwright install chromium`.

## Running the GUI

```bash
.venv/bin/python -m src.gui.app --config searches.json
```

The GUI binds to `127.0.0.1:8765` and opens the default browser. Opening the GUI
does not scan marketplaces. Use **Scan now** or **Start monitoring** explicitly.
For a headless machine:

```bash
.venv/bin/python -m src.gui.app --config searches.json --no-browser
```

Useful overrides include `--database`, `--output-dir`, `--debug-dir`, `--host`,
and `--port`. See all options with:

```bash
.venv/bin/python -m src.gui.app --help
```

### Desktop shell (Linux first)

Desktop mode reuses the same local server, frontend, configuration, and SQLite
database inside a native pywebview window. Qt is the manually accepted and default
dependency path for Desktop v1 on Linux. Install it with:

```bash
.venv/bin/python -m pip install -r requirements-desktop.txt
```

Launch it from the same working directory and use the same data-path options as
browser mode:

```bash
.venv/bin/python -m src.desktop --gui qt
.venv/bin/python -m src.desktop --debug
```

`requirements-desktop.txt` installs `pywebview[qt]` only on Linux. On future Windows
or macOS installations it installs base pywebview instead of forcing Linux Qt
packages. The launcher can also choose the installed backend automatically, so
`python -m src.desktop` remains valid when the environment has one working backend.

GTK remains an optional Linux alternative. Install the Python GTK bindings,
WebKitGTK, and any pywebview GTK requirements appropriate to the distribution,
then launch with `python -m src.desktop --gui gtk`. Package names vary by Linux
distribution; the application does not download system GUI runtimes itself.

Desktop mode binds only to `127.0.0.1` on an OS-selected ephemeral port and uses a
random one-launch session. Browser development mode remains independent and does
not require pywebview.

### Browser-assisted retail

Browser-assisted capture is manual and opt-in. Install Playwright Firefox, run
the GUI normally, select a normalized product on Market, then use the
**Browser-assisted retail** panel:

1. Save a canonical DNS, Ozon, or Wildberries product URL.
2. Select Firefox (recommended) or Chromium, then click **Open browser**. Each
   engine uses its own dedicated local profile under `data/playwright/`.
3. Click the mapping's **Open** button. Complete normal region selection, dialogs,
   or a site challenge yourself if necessary.
4. Optionally enter a region only after confirming it in the visible site.
5. Click **Capture**. Capture inspects the already-loaded page and relevant
   first-party JSON responses; it makes no hidden HTTP request or reload.

Market page loads and searches never launch a retail browser. Browser capture is not a
CAPTCHA solver, stealth transport, or scheduled crawler. **Reset profile** closes
the retail browser and deletes only its dedicated profile after confirmation.

## Running the backend

Run one pass:

```bash
.venv/bin/python -m src.backend --config searches.json
```

Run continuous background monitoring using each search's configured interval:

```bash
.venv/bin/python -m src.backend --config searches.json --loop
```

## Configuration

`searches.example.json` documents safe, disabled examples. Each search selects a
source and URL and may define local include/exclude terms, brands, price bounds,
target price, scan interval, jitter, block retry/cooldown behavior, and Avito
transport profile.

The examples include one conservative Khabarovsk Avito query each for processors,
SSDs, and motherboards. They are disabled by default, run no more often than every
30 minutes when enabled, and filter locally for common Ryzen/Core, SATA/M.2/NVMe,
and AM4/AM5/LGA1200/LGA1700/chipset terms. Copy or merge only the profiles you
need into `searches.json`; they retain the normal cooldown and block handling.

Direct routing is the default. A user-supplied proxy URL is accepted only when a
search explicitly selects proxy routing. Proxy URLs may contain credentials, so
real configuration files and their backups must never be committed.

Application settings edited in the GUI are stored in the local SQLite database.
Retail product mappings are also local database data and may contain private or
personal URLs.

## Local data and exports

Default runtime paths, relative to the working directory, are:

- Database and app settings: `data/resell-monitor.db`
- Persistent Playwright data: `data/playwright/`
- Browser-assisted Firefox profile: `data/playwright/retail-firefox-profile/`
- Browser-assisted Chromium profile: `data/playwright/retail-chromium-profile/`
- User search configuration: `searches.json`
- Reports and source diagnostics: `output/`
- Retail diagnostics: `debug/retail/`

These paths are ignored by Git. Do not publish databases, browser profiles,
debug responses, generated reports, or configuration backups.

Automatic exports use collision-safe local-time names such as
`listings_gpu_2026-08-10_180512.txt`. TXT, JSON, and HTML include normalized
identity, availability/condition, compatible used-market evidence, decomposed
deal/confidence/liquidity/risk scores, priority/verdict, and review reasons.

## Market and availability semantics

Historical price observations remain part of market snapshots even after a
listing becomes stale or unavailable. Current opportunities are stricter:
archived and disappeared listings are never actionable, while stale and unknown
listings are excluded by default. This prevents an old cheap listing from being
presented as a current buying opportunity.

Candidate validation runs before normal persistence. Source health therefore
tracks raw, valid, rejected, and priced items; an HTTP 200 response with mostly
navigation/UI artifacts is degraded rather than healthy. Used-market ranking is
cross-source, excludes the candidate itself, faulty or price-ambiguous offers,
and conservative near-duplicates. A sufficiently large exact-location pool is
preferred; otherwise the broader pool is labeled and confidence is reduced.

## Retail diagnostics

Retail diagnostics make one live provider request and save sanitized output.
They are not part of the offline test suite. Run them only deliberately:

```bash
.venv/bin/python -m src.debug_retail dns --query "RTX 3060 12GB" --region Khabarovsk
.venv/bin/python -m src.debug_retail dns --key 'gpu:rtx-3060:12gb' --url 'https://www.dns-shop.ru/product/0123456789abcdef/example-product/' --region Khabarovsk
.venv/bin/python -m src.debug_retail ozon --query "RTX 3060 12GB" --region Khabarovsk
.venv/bin/python -m src.debug_retail ozon --key 'gpu:rtx-3060:12gb' --url 'https://www.ozon.ru/product/example-1234567890/' --region Khabarovsk
.venv/bin/python -m src.debug_retail wildberries --query "RTX 3060 12GB" --region Khabarovsk
```

Wildberries also accepts `--product-id <nmId>` for one exact mapped-card request.
Its numeric destination can be supplied without pretending the configured city
name is itself a WB ID, for example `--region "Region name; wb_dest=-123"` after
confirming the current destination value in a normal browser session.

Ozon mapped diagnostics validate a canonical HTTPS product URL and make one
bounded logical retrieval. Ozon search is intended for explicit discovery;
scheduled monitoring should use a reviewed product mapping. Redirect diagnostics
retain host/path, parameter names, and cookie names only—never their values.

DNS mapped diagnostics likewise require a canonical `/product/<id>/<slug>/`
HTTPS URL. DNS currently presents a Qrator challenge on the tested route, so its
ordinary HTTP provider remains blocked/experimental. The configured region is
recorded as unresolved scope unless a future normal consumer path provides
verifiable DNS locality context.

Use `--url` for an explicit mapped product page and `--output-dir` to change the
diagnostic directory. Current provider research and limitations are documented
in `docs/retail-research.md`.

Marketplace source diagnostics are similarly explicit and make live requests;
see `.venv/bin/python -m src.debug_source --help` before using them.

## Running tests

The complete test suite is offline and uses saved fixtures or fake transports:

```bash
.venv/bin/python -m unittest discover -s tests -p 'test_*.py' -v
```

## Project structure

```text
src/
  analytics/          product normalization, market statistics, availability,
                      condition assessment, and resale scoring
  gui/                local HTTP application and static web interface
  reporting/          JSON, TXT, and HTML exports
  retail_providers/   experimental retail provider adapters
  sources/            marketplace-specific source adapters and transports
  storage/            versioned SQLite repository
  models.py           shared marketplace-independent domain models
  monitor.py          scan orchestration
tests/                offline unit and regression tests plus saved fixtures
docs/                 design and provider research notes
```

## Known limitations

- Marketplace and retail HTML/JSON structures can change without notice.
- Youla retrieval remains experimental/degraded when product data is not present
  in embedded state.
- Retail providers may be blocked and are not guaranteed to return usable data.
- Product normalization intentionally excludes ambiguous listings.
- Availability can be confirmed as archived only when an existing source result
  includes an explicit archive/unavailable signal; disappearance is kept distinct.
- The current paths and shell examples are Linux-oriented. Windows packaging and
  Docker deployment have not yet been implemented.
- There is no CAPTCHA bypass, proxy rotation, or high-frequency pagination.

## Third-party reference and licensing status

Public projects were studied only at the architectural level. No source code was
copied from `Duff89/parser_avito`; that repository had no license and was treated
as reference-only.

This project is available under the MIT License; see `LICENSE`. Its direct
Python dependencies also use permissive licenses.
