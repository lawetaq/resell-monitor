from __future__ import annotations

import json
import re
from dataclasses import asdict
from typing import Iterable
from pathlib import Path
from urllib.parse import urlencode, urlsplit, urlunsplit

from src.models import LocationMode, LocationProfile, SearchConfig
from src.resources import resource_path


def _load_registry() -> dict[str, LocationProfile]:
    rows = json.loads(resource_path("src/location_registry.json").read_text(encoding="utf-8"))
    return {str(row["id"]): LocationProfile(
        id=str(row["id"]), display_name=str(row["display_name"]), country="RU",
        source_tokens=row.get("source_tokens") or {}, aliases=tuple(row.get("aliases") or ()),
    ) for row in rows}


KNOWN_LOCATIONS = _load_registry()
DEFAULT_LOCATION = KNOWN_LOCATIONS["khabarovsk"]
SOURCE_HOSTS = {"avito": ("avito.ru",), "farpost": ("farpost.ru",), "youla": ("youla.ru",)}
_RESERVED_AVITO = {"all", "rossiya", "web", "search", "profile", "business"}


def profile_to_json(profile: LocationProfile) -> str:
    return json.dumps(asdict(profile), ensure_ascii=False, sort_keys=True)


def profile_from_json(value: str) -> LocationProfile:
    if value in KNOWN_LOCATIONS:
        return KNOWN_LOCATIONS[value]
    data = json.loads(value)
    return LocationProfile(str(data["id"]), str(data["display_name"]), data.get("country"),
                           data.get("source_tokens") or {}, tuple(data.get("aliases") or ()))


def profile_setting_value(profile: LocationProfile) -> str:
    return profile.id if profile.id in KNOWN_LOCATIONS and profile == KNOWN_LOCATIONS[profile.id] else profile_to_json(profile)


def search_locations(query: str = "") -> list[LocationProfile]:
    needle = query.casefold().strip()
    profiles = sorted(KNOWN_LOCATIONS.values(), key=lambda item: item.display_name.casefold())
    if not needle:
        return profiles
    return [profile for profile in profiles if any(
        needle in value.casefold() for value in (profile.id, profile.display_name, *profile.aliases)
    )]


def source_resolution(profile: LocationProfile) -> dict[str, str]:
    return {
        "avito": "ready" if profile.source_tokens.get("avito") else "not-configured",
        "farpost": "ready" if profile.source_tokens.get("farpost") else "not-configured",
        "youla": "unsupported",
    }


def validate_marketplace_url(url: str, source: str | None = None) -> tuple[str, str]:
    parsed = urlsplit(url.strip())
    if parsed.scheme.lower() != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("marketplace URL must be an HTTPS URL without credentials")
    host = parsed.hostname.casefold().rstrip(".")
    detected = next((name for name, domains in SOURCE_HOSTS.items()
                     if any(host == domain or host.endswith("." + domain) for domain in domains)), None)
    if detected is None or source is not None and detected != source:
        raise ValueError("marketplace URL hostname is not allowed for this source")
    return detected, urlunsplit(("https", parsed.netloc, parsed.path or "/", parsed.query, ""))


def detect_location_from_url(url: str, source: str | None = None,
                             profiles: Iterable[LocationProfile] = ()) -> tuple[str, LocationProfile | None]:
    detected_source, clean = validate_marketplace_url(url, source)
    parsed = urlsplit(clean)
    segments = [segment.casefold() for segment in parsed.path.split("/") if segment]
    known = {profile.source_tokens.get(detected_source, "").casefold(): profile
             for profile in [*KNOWN_LOCATIONS.values(), *profiles]
             if profile.source_tokens.get(detected_source)}
    token = segments[0] if segments else ""
    if detected_source == "avito" and token in _RESERVED_AVITO:
        return detected_source, None
    return detected_source, known.get(token)


def learn_location_from_url(display_name: str, url: str, *, source: str,
                            location_id: str) -> LocationProfile:
    detected_source, clean = validate_marketplace_url(url, source)
    segments = [segment for segment in urlsplit(clean).path.split("/") if segment]
    if not segments or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", segments[0]):
        raise ValueError("URL does not contain a safe source location token")
    if detected_source == "avito" and segments[0].casefold() in _RESERVED_AVITO:
        raise ValueError("URL does not identify a specific Avito location")
    return LocationProfile(location_id.strip(), display_name.strip(), "RU", {source: segments[0]})


def build_search_url(search: SearchConfig, default_location: LocationProfile) -> str:
    if not search.preset_id:
        return validate_marketplace_url(search.url, search.source)[1]
    from src.config import SEARCH_PRESETS
    try:
        preset = SEARCH_PRESETS[search.preset_id]
    except KeyError as error:
        raise ValueError(f"unknown search preset: {search.preset_id}") from error
    if preset.get("source") != search.source:
        raise ValueError(f"preset {search.preset_id} does not support {search.source}")
    location = default_location if search.location_mode == LocationMode.DEFAULT else search.specific_location
    if search.location_mode == LocationMode.SPECIFIC and location is None:
        raise ValueError("specific location mode requires a location profile")
    if search.source == "avito":
        token = "rossiya" if search.location_mode == LocationMode.ALL else location.source_tokens.get("avito") if location else None
        if not token:
            raise ValueError(f"Avito location is unresolved for {location.display_name if location else 'search'}")
        query = urlencode({"q": str(preset["query"])}) if preset.get("query") else ""
        return urlunsplit(("https", "www.avito.ru", f"/{token}/{preset['path']}", query, ""))
    if search.source == "farpost":
        token = "" if search.location_mode == LocationMode.ALL else location.source_tokens.get("farpost") if location else None
        if search.location_mode != LocationMode.ALL and not token:
            raise ValueError(f"FarPost location is unresolved for {location.display_name if location else 'search'}")
        prefix = f"/{token}" if token else ""
        query = urlencode({"query": str(preset["query"])}) if preset.get("query") else ""
        return urlunsplit(("https", "www.farpost.ru", f"{prefix}/{preset['path']}", query, ""))
    raise ValueError(f"location-aware presets are not supported for {search.source}")
