from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from src.models import LocationMode, LocationProfile, SearchConfig


SEARCH_PRESETS: dict[str, dict[str, object]] = {
    "farpost_gpu": {"label": "GPU", "source": "farpost", "name": "FarPost GPUs",
                    "path": "tech/computers/components/video/", "query": "видеокарта",
                    "include_terms": ["rtx", "gtx", "geforce", "radeon", "rx", "видеокарта"]},
    "gpu": {"label": "GPU", "source": "avito", "name": "Avito GPUs", "path": "tovary_dlya_kompyutera/komplektuyuschie/videokarty-ASgBAgICAkTGB~pm7gmmZw", "query": None,
            "include_terms": ["rtx", "gtx", "geforce", "radeon", "rx", "видеокарта"]},
    "ram": {"label": "RAM", "source": "avito", "name": "Avito RAM", "path": "tovary_dlya_kompyutera/komplektuyuschie", "query": "ddr4 оперативная память",
            "include_terms": ["ddr", "оперативная память", "озу", "sodimm", "rdimm"]},
    "cpu_ryzen": {"label": "CPU — Ryzen", "source": "avito", "name": "Avito Ryzen", "path": "tovary_dlya_kompyutera/komplektuyuschie", "query": "ryzen",
                  "include_terms": ["ryzen", "ryzen 5", "ryzen 7"]},
    "cpu_intel": {"label": "CPU — Intel Core", "source": "avito", "name": "Avito Intel Core", "path": "tovary_dlya_kompyutera/komplektuyuschie", "query": "intel core",
                  "include_terms": ["core i3", "core i5", "core i7"]},
    "ssd": {"label": "SSD", "source": "avito", "name": "Avito SSD", "path": "tovary_dlya_kompyutera/komplektuyuschie", "query": "ssd",
            "include_terms": ["ssd", "sata", "2.5", "m.2", "nvme"]},
    "motherboard": {"label": "Motherboard", "source": "avito", "name": "Avito motherboards", "path": "tovary_dlya_kompyutera/komplektuyuschie", "query": "материнская плата",
                    "include_terms": ["am4", "am5", "lga1200", "lga 1200", "lga1700", "lga 1700",
                                      "a520", "b450", "b550", "x570", "b560", "b660", "b760"]},
}


def search_presets() -> list[dict[str, object]]:
    common = {"enabled": False, "interval_seconds": 1800, "jitter_seconds": 90,
              "exclude_terms": ["ремонт", "неисправен", "неисправна", "на запчасти"],
              "brands": [], "min_price": None, "max_price": 35000, "target_price": None,
              "block_retry_delay_seconds": 60, "block_cooldown_seconds": 1800,
              "max_block_retries": 1, "network_route": "direct", "proxy_url": None,
              "avito_impersonation": "chrome", "avito_session_mode": "persistent"}
    return [{"id": preset_id, "preset_id": preset_id, "location_mode": "default", **common, **values} for preset_id, values in SEARCH_PRESETS.items()]


def load_searches(path: Path) -> list[SearchConfig]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return parse_searches(raw)


def parse_searches(raw: Any) -> list[SearchConfig]:
    if not isinstance(raw, list):
        raise ValueError("search configuration must be a JSON array")
    searches: list[SearchConfig] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("each search must be an object")
        data = dict(item)
        data["include_terms"] = _terms(data.get("include_terms", ()), "include_terms")
        data["exclude_terms"] = _terms(data.get("exclude_terms", ()), "exclude_terms")
        data["brands"] = _terms(data.get("brands", ()), "brands")
        data["location_mode"] = LocationMode(data.get("location_mode", "default"))
        profile = data.get("specific_location")
        if isinstance(profile, dict):
            data["specific_location"] = LocationProfile(**profile)
        search = SearchConfig(**data)
        if not search.name.strip() or not search.source.strip():
            raise ValueError("search name and source must not be empty")
        parsed_url = urlsplit(search.url)
        if not search.preset_id and (parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc):
            raise ValueError(f"{search.name}: url must be an absolute HTTP(S) URL")
        if search.interval_seconds < 60:
            raise ValueError(f"{search.name}: interval_seconds must be at least 60")
        if not 0 <= search.jitter_seconds <= search.interval_seconds // 2:
            raise ValueError(
                f"{search.name}: jitter_seconds must be between 0 and half the interval"
            )
        if search.location_mode == LocationMode.SPECIFIC and search.specific_location is None:
            raise ValueError(f"{search.name}: specific location mode requires specific_location")
        if search.block_retry_delay_seconds < 0:
            raise ValueError(
                f"{search.name}: block_retry_delay_seconds must be non-negative"
            )
        if search.block_cooldown_seconds < 60:
            raise ValueError(
                f"{search.name}: block_cooldown_seconds must be at least 60"
            )
        if search.max_block_retries not in {0, 1}:
            raise ValueError(f"{search.name}: max_block_retries must be 0 or 1")
        if search.network_route not in {"direct", "proxy"}:
            raise ValueError(f"{search.name}: network_route must be direct or proxy")
        if search.network_route == "proxy" and not search.proxy_url:
            raise ValueError(f"{search.name}: proxy route requires proxy_url")
        if search.network_route == "direct" and search.proxy_url:
            raise ValueError(f"{search.name}: direct route must not define proxy_url")
        if search.proxy_url:
            proxy = urlsplit(search.proxy_url)
            if proxy.scheme.lower() not in {
                "http",
                "https",
                "socks4",
                "socks4a",
                "socks5",
                "socks5h",
            } or not proxy.hostname:
                raise ValueError(f"{search.name}: unsupported proxy_url")
            if search.source in {"farpost", "youla"} and proxy.scheme.lower().startswith(
                "socks"
            ):
                raise ValueError(
                    f"{search.name}: SOCKS is not supported by the {search.source} "
                    "Requests transport; use an HTTP(S) proxy"
                )
        if not search.avito_impersonation.strip():
            raise ValueError(f"{search.name}: avito_impersonation must not be empty")
        if search.avito_session_mode not in {"persistent", "fresh"}:
            raise ValueError(
                f"{search.name}: avito_session_mode must be persistent or fresh"
            )
        for field_name in ("min_price", "max_price", "target_price"):
            value = getattr(search, field_name)
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
                raise ValueError(f"{search.name}: {field_name} must be a non-negative integer")
        if search.target_price == 0:
            raise ValueError(f"{search.name}: target_price must be greater than zero")
        if search.min_price is not None and search.max_price is not None and search.min_price > search.max_price:
            raise ValueError(f"{search.name}: min_price must not exceed max_price")
        searches.append(search)
    return searches


def save_searches(path: Path, searches: list[SearchConfig]) -> None:
    """Atomically persist validated search configuration without logging secrets."""

    validated = parse_searches([asdict(search) for search in searches])
    payload = json.dumps(
        [asdict(search) for search in validated],
        ensure_ascii=False,
        indent=2,
    ) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as temporary:
            temporary.write(payload)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def _terms(value: Any, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or not all(isinstance(term, str) and term.strip() for term in value):
        raise ValueError(f"{field_name} must be an array of non-empty strings")
    return tuple(term.strip() for term in value)
