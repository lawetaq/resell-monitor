"""Explicit, user-triggered GitHub release availability checks."""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

from src.project import (
    GITHUB_OWNER,
    GITHUB_RELEASES_API_URL,
    GITHUB_REPOSITORY,
)
from src.version import RELEASE_CHANNEL, __version__


UPDATE_TIMEOUT_SECONDS = 5.0
MAX_RESPONSE_BYTES = 1_000_000
_TAG_PATTERN = re.compile(
    r"^v?(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\."
    r"(?P<patch>0|[1-9]\d*)(?:-(?P<channel>alpha|beta))?$",
    re.IGNORECASE,
)
_CHANNEL_ORDER = {"alpha": 0, "beta": 1, "stable": 2}


@dataclass(frozen=True, order=True, slots=True)
class ReleaseVersion:
    major: int
    minor: int
    patch: int
    maturity: int

    @property
    def numeric(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"

    @property
    def channel(self) -> str:
        return next(
            name for name, order in _CHANNEL_ORDER.items() if order == self.maturity
        )

    @property
    def display(self) -> str:
        return self.numeric if self.channel == "stable" else f"{self.numeric}-{self.channel}"


def parse_release_version(tag: str) -> ReleaseVersion | None:
    match = _TAG_PATTERN.fullmatch(tag.strip())
    if match is None:
        return None
    channel = (match.group("channel") or "stable").casefold()
    return ReleaseVersion(
        int(match.group("major")),
        int(match.group("minor")),
        int(match.group("patch")),
        _CHANNEL_ORDER[channel],
    )


def _is_project_release_url(url: str, tag: str) -> bool:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    expected_path = (
        f"/{GITHUB_OWNER}/{GITHUB_REPOSITORY}/releases/tag/{quote(tag, safe='')}"
    )
    return (
        parsed.scheme == "https"
        and (parsed.hostname or "").casefold() == "github.com"
        and parsed.username is None
        and parsed.password is None
        and parsed.port in {None, 443}
        and parsed.path == expected_path
        and not parsed.query
        and not parsed.fragment
    )


def _fetch_releases() -> object:
    request = Request(
        GITHUB_RELEASES_API_URL,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"ResellMonitor/{__version__}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        method="GET",
    )
    with urlopen(request, timeout=UPDATE_TIMEOUT_SECONDS) as response:
        content = response.read(MAX_RESPONSE_BYTES + 1)
    if len(content) > MAX_RESPONSE_BYTES:
        raise ValueError("GitHub response is too large")
    return json.loads(content)


def check_for_updates(
    fetch_releases: Callable[[], object] = _fetch_releases,
) -> dict[str, object]:
    """Check fixed project releases and return only normalized public fields."""

    current = parse_release_version(f"{__version__}-{RELEASE_CHANNEL}")
    if current is None:  # Canonical local constants should never reach this branch.
        return {"status": "api_error", "current_version": __version__}
    try:
        payload = fetch_releases()
        if not isinstance(payload, list):
            raise ValueError("GitHub releases response must be an array")
        candidates: list[tuple[ReleaseVersion, str, str]] = []
        for item in payload:
            if not isinstance(item, dict) or bool(item.get("draft")):
                continue
            tag = item.get("tag_name")
            url = item.get("html_url")
            if not isinstance(tag, str) or not isinstance(url, str):
                continue
            version = parse_release_version(tag)
            if version is None or not _is_project_release_url(url, tag):
                continue
            name = item.get("name")
            candidates.append((version, url, str(name).strip() if name else tag))
        if not candidates:
            return {"status": "no_release", "current_version": __version__}
        latest, release_url, release_name = max(candidates, key=lambda row: row[0])
        result: dict[str, object] = {
            "status": "update_available" if latest > current else "up_to_date",
            "current_version": __version__,
            "latest_version": latest.display,
            "latest_channel": latest.channel,
            "release_name": release_name,
        }
        if latest > current:
            result["release_url"] = release_url
        return result
    except (HTTPError, URLError, TimeoutError, OSError, UnicodeError, ValueError):
        return {"status": "api_error", "current_version": __version__}
