# Resell Monitor

## Project goal

Build a local marketplace monitoring application for finding potentially
underpriced PC components for resale.

Primary region: Khabarovsk.

Initial marketplaces:
- Avito
- FarPost
- Youla

The application itself does not need AI-based valuation.
Listings will later be reviewed manually.

## Architecture

Keep marketplace-specific logic isolated in:

- `src/sources/`

Shared domain models belong in:

- `src/models.py`

Persistence belongs in:

- `src/storage/`

Reports and exports belong in:

- `src/reporting/`

Notifications belong in:

- `src/notifiers/`

Application orchestration must not depend on Avito-specific field names.

Each marketplace source should convert its raw data into the same shared
`Listing` model.

## Avito strategy

Preferred retrieval order:

1. Try ordinary HTTP requests.
2. Parse Avito embedded frontend JSON when available.
3. Use Playwright as a fallback only when normal HTTP retrieval is insufficient.

Do not use aggressive scraping techniques.

Do not implement CAPTCHA bypass.

Do not implement proxy rotation unless explicitly requested later.

Do not use stealth or anti-detection libraries unless there is a demonstrated
technical need and it is explicitly approved.

Use conservative request frequency.

## Future functionality

Planned features include:

- multiple search configurations
- SQLite persistence
- duplicate detection
- price history
- first-seen and last-seen timestamps
- filtering
- ranking candidates by simple rules
- HTML reports
- TXT/JSON export
- GUI
- VK notifications
- scheduled scanning
- FarPost source
- Youla source

## Development rules

- Python 3.13
- Use type hints.
- Prefer small, testable modules.
- Add tests for parsing and important business logic.
- Marketplace adapters may use different transport mechanisms.
- Do not leak marketplace-specific raw dictionaries outside source adapters.
- Do not commit secrets, cookies, credentials, databases, logs, or generated reports.
- Avoid unnecessary dependencies.
- Prefer standard library solutions when reasonable.
- Handle network and parsing failures explicitly.
- Do not silently swallow errors.
- Preserve saved HTML fixtures when useful for offline parser tests.

## Current priority

The current priority is Avito MVP.

Do not implement FarPost, Youla, GUI, VK notifications, scheduling, or advanced
filtering until the Avito MVP is working and tested.
