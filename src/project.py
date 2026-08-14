"""Central factual project metadata used by the UI and release integration."""

from __future__ import annotations

from src.version import RELEASE_CHANNEL, __version__


GITHUB_OWNER = "lawetaq"
GITHUB_REPOSITORY = "resell-monitor"
REPOSITORY_URL = f"https://github.com/{GITHUB_OWNER}/{GITHUB_REPOSITORY}"
RELEASES_URL = f"{REPOSITORY_URL}/releases"
ISSUES_URL = f"{REPOSITORY_URL}/issues"
CHANGELOG_URL = f"{REPOSITORY_URL}/blob/main/CHANGELOG.md"
LICENSE_URL = f"{REPOSITORY_URL}/blob/main/LICENSE"
GITHUB_RELEASES_API_URL = (
    f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPOSITORY}/releases"
    "?per_page=20"
)


def project_info() -> dict[str, object]:
    """Return the small public metadata structure consumed by Settings."""

    return {
        "name": "Resell Monitor",
        "version": __version__,
        "release_channel": RELEASE_CHANNEL,
        "urls": {
            "repository": REPOSITORY_URL,
            "releases": RELEASES_URL,
            "issues": ISSUES_URL,
            "changelog": CHANGELOG_URL,
            "license": LICENSE_URL,
        },
    }
