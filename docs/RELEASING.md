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
12. Upload the AppImage and SHA256 file from `dist/release/`.

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

## First public alpha publication

After the release state and final locally built artifacts are approved, create
the annotated tag manually:

```bash
git tag -a v0.1.0-alpha -m "Resell Monitor 0.1.0 Alpha"
git push origin v0.1.0-alpha
```

Then create a GitHub Release with:

- Tag: `v0.1.0-alpha`
- Title: `Resell Monitor 0.1.0 Alpha`
- Release type: **Pre-release**
- Description: the reviewed contents of `docs/releases/0.1.0-alpha.md`
- Assets: `ResellMonitor-0.1.0-x86_64.AppImage` and
  `ResellMonitor-0.1.0-x86_64.AppImage.sha256`

The installer, uninstaller, desktop template, icons, and local
`RELEASE_NOTES.md` remain useful in the reproducible local bundle but are not
required public downloads for the AppImage's GUI installation flow.

## Post-release update-check acceptance

Before the GitHub Release exists, a manual update check may correctly report no
published release. After `v0.1.0-alpha` is published:

1. Launch Resell Monitor 0.1.0 Alpha.
2. Open Settings → About.
3. Confirm no update request occurred automatically during startup.
4. Press **Check for updates**.
5. Confirm the result reports the installed version as current.
6. If a release link is shown, confirm it opens the exact published GitHub
   release in the system browser.

This exercises the application, GitHub Releases API, semantic release parser,
external URL policy, and About UI end to end. For a later `v0.1.1-alpha`
release, repeat from 0.1.0 and confirm **Update available** plus **View release**;
no automatic download should occur.

## Future automation boundary

GitHub Actions release automation remains deferred for this first release. A
later CI-friendly flow can use an explicit tag push to run offline tests, build
the Linux AppImage, generate and verify its checksum, and attach the two public
assets to a GitHub Release. It must continue to use the existing local scripts
rather than duplicate packaging logic.
