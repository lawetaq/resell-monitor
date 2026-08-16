# Changelog

Notable user-visible changes to Resell Monitor are documented here.

## [Unreleased]

## [0.1.0] - 2026-08-14

### Added

- Local marketplace monitoring for Avito, FarPost, and Youla through shared listing models.
- Saved searches, listing thumbnails, duplicate detection, price history, and lifecycle tracking.
- Deterministic deal ranking, comparable-market analytics, condition signals, and review reasons.
- TXT, JSON, and HTML exports.
- Russian and English interfaces with the complete application theme set.
- Native Linux desktop shell and Linux x86_64 AppImage distribution.
- Optional first-run user-level desktop-menu integration, with installation and removal helpers as a fallback.
- Persistent platform-native data, configuration, cache, exports, and logs.
- Verified database backups before schema migration.
- Experimental browser-assisted retail evidence for supported retail sources.
- Branded About and Support resources with manual GitHub release checking.

### Changed

- Packaged application files are fully separated from mutable user data.
- AppImage packaging now contains AppStream metadata.
- Read-only Settings sections no longer display the global save action.

### Fixed

- Desktop shutdown closes the local listener without leaving an application process behind.

This is the first public alpha. Linux distribution coverage and marketplace access
remain limited; individual sources may be degraded by site or anti-bot changes.
