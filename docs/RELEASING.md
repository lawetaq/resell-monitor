# Releasing Resell Monitor

Releases are currently built and published manually so the exact first Linux
artifacts can be smoke-tested before release automation is introduced.

## Tag convention

The numeric application version lives only in `src/version.py`. Release maturity
is expressed in tags and release titles:

- `v0.1.0-alpha`
- `v0.2.0-alpha`
- `v0.3.0-beta`
- `v1.0.0`

## Release checklist

1. Ensure the working tree is clean and the intended numeric version is in `src/version.py`.
2. Run the complete offline test suite.
3. Build the release bundle with an explicitly supplied `appimagetool`.
4. Run release validation (the build wrapper also runs it automatically).
5. Manually smoke-test the AppImage outside the repository.
6. Verify the SHA256 checksum with `sha256sum -c`.
7. Review `CHANGELOG.md` and the versioned release notes.
8. Test user installation, menu launch, reinstall, and uninstall on KDE or GNOME.
9. Commit approved release metadata and code changes.
10. Create the release tag manually.
11. Create the GitHub Release manually and use the versioned release notes.
12. Upload every file from `dist/release/` without databases or user configuration.

Build command:

```bash
APPIMAGETOOL=/absolute/path/to/appimagetool-x86_64.AppImage \
PYTHON=.venv/bin/python scripts/build_release_linux.sh
```

Validation can be repeated without rebuilding:

```bash
PYTHON=.venv/bin/python scripts/validate_release_linux.sh dist/release
```

The scripts do not create commits, tags, releases, or uploads. A GitHub Actions
release workflow is intentionally deferred until the manual Linux release has
been validated across the target desktop workflow.
