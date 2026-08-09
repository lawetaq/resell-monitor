from __future__ import annotations

import threading
import time
import logging
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable

from src.backend import _build_source
from src.config import load_searches, parse_searches, save_searches
from src.models import ListingStatus, SearchConfig
from src.monitor import Monitor, SourceScan
from src.reporting import export_html, export_json, export_txt
from src.scheduler import SearchScheduler
from src.storage import ListingRepository, SearchAccessState
from src.retail_monitor import RetailMonitor
from src.retail_providers import DnsRetailProvider, OzonRetailProvider, WildberriesRetailProvider

ScanRunner = Callable[[list[SearchConfig], Callable[[str], None]], list[SourceScan]]
LOGGER = logging.getLogger(__name__)


class GuiService:
    """Thread-safe GUI boundary; construction never creates a source or scans."""

    def __init__(
        self,
        *,
        config_path: Path,
        database_path: Path,
        output_dir: Path,
        debug_dir: Path | None = None,
        scan_runner: ScanRunner | None = None,
    ) -> None:
        self.config_path = config_path
        self.database_path = database_path
        self.output_dir = output_dir
        self.debug_dir = debug_dir
        self._external_scan_runner = scan_runner
        self._state_lock = threading.RLock()
        self._scan_lock = threading.Lock()
        self._retail_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._monitor_thread: threading.Thread | None = None
        self._monitoring = False
        self._runtime_state = "Idle"
        self._last_scan_at: datetime | None = None
        self._last_error: str | None = None
        self._scheduler = SearchScheduler()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        with ListingRepository(self.database_path):
            pass

    def searches(self, *, include_secrets: bool = False) -> list[dict[str, object]]:
        searches = self._load_searches()
        with ListingRepository(self.database_path) as repository:
            states = {
                (state.source, state.search_name, state.search_url): state
                for state in repository.search_access_states()
            }
        return [
            self._search_public(
                search,
                state=states.get(
                    (search.source, search.name, search.url),
                    SearchAccessState(search.source, search.name, search.url),
                ),
                include_secrets=include_secrets,
            )
            for search in searches
        ]

    def create_search(self, payload: dict[str, object]) -> dict[str, object]:
        searches = self._load_searches()
        candidate = parse_searches([payload])[0]
        if any(search.name == candidate.name for search in searches):
            raise ValueError(f"search named {candidate.name!r} already exists")
        searches.append(candidate)
        save_searches(self.config_path, searches)
        return self._search_public(
            candidate,
            state=SearchAccessState(candidate.source, candidate.name, candidate.url),
        )

    def update_search(
        self, current_name: str, payload: dict[str, object]
    ) -> dict[str, object]:
        searches = self._load_searches()
        index = self._search_index(searches, current_name)
        existing = searches[index]
        merged = dict(payload)
        if (
            merged.get("network_route", existing.network_route) == "proxy"
            and not merged.get("proxy_url")
            and existing.proxy_url
        ):
            merged["proxy_url"] = existing.proxy_url
        candidate = parse_searches([merged])[0]
        if any(
            search.name == candidate.name and position != index
            for position, search in enumerate(searches)
        ):
            raise ValueError(f"search named {candidate.name!r} already exists")
        searches[index] = candidate
        save_searches(self.config_path, searches)
        return self._search_public(
            candidate,
            state=SearchAccessState(candidate.source, candidate.name, candidate.url),
        )

    def delete_search(self, name: str) -> None:
        searches = self._load_searches()
        index = self._search_index(searches, name)
        del searches[index]
        save_searches(self.config_path, searches)

    def set_search_enabled(self, name: str, enabled: bool) -> dict[str, object]:
        searches = self._load_searches()
        index = self._search_index(searches, name)
        searches[index] = replace(searches[index], enabled=enabled)
        save_searches(self.config_path, searches)
        search = searches[index]
        return self._search_public(
            search,
            state=SearchAccessState(search.source, search.name, search.url),
        )

    def listings(self, filters: dict[str, object] | None = None) -> list[dict[str, object]]:
        values = filters or {}
        with ListingRepository(self.database_path) as repository:
            return repository.listing_rows(
                source=_string_or_none(values.get("source")),
                search_name=_string_or_none(values.get("search")),
                status=_string_or_none(values.get("status")),
                new_only=_truthy(values.get("new_only")),
                price_drops=_truthy(values.get("price_drops")),
                min_price=_integer_or_none(values.get("min_price")),
                max_price=_integer_or_none(values.get("max_price")),
                text=_string_or_none(values.get("text")),
                availability=_string_or_none(values.get("availability")),
            )

    def listing_detail(self, source: str, external_id: str) -> dict[str, object]:
        with ListingRepository(self.database_path) as repository:
            return repository.listing_detail(source, external_id)

    def set_listing_status(
        self, source: str, external_id: str, status: str
    ) -> dict[str, object]:
        try:
            normalized = ListingStatus(status)
        except ValueError as error:
            raise ValueError(f"unsupported listing status: {status}") from error
        with ListingRepository(self.database_path) as repository:
            repository.set_status(source, external_id, normalized)
            return repository.listing_detail(source, external_id)

    def history(self, *, limit: int = 100) -> list[dict[str, object]]:
        with ListingRepository(self.database_path) as repository:
            return repository.monitoring_events(limit=max(1, min(limit, 500)))

    def market_search(
        self, query: str = "", product_category: str | None = None
    ) -> list[dict[str, object]]:
        """Search normalized local observations only; never constructs a source."""
        with ListingRepository(self.database_path) as repository:
            return repository.market_search(
                query.strip(), product_category=product_category or None
            )

    def market_product(self, comparable_key: str, range_name: str = "30D") -> dict[str, object]:
        ranges = {"7D": 7, "30D": 30, "90D": 90, "ALL": None}
        if range_name not in ranges:
            raise ValueError("range must be 7D, 30D, 90D, or ALL")
        with ListingRepository(self.database_path) as repository:
            summary = repository.market_summary(comparable_key)
            if summary is None:
                raise KeyError(comparable_key)
            return {
                "summary": summary,
                "snapshots": repository.market_snapshots(
                    comparable_key, days=ranges[range_name]
                ),
                "retail_observations": repository.retail_observations(comparable_key),
                "retail_summary": repository.retail_summary(comparable_key),
                "retail_history": repository.retail_history(
                    comparable_key, days=ranges[range_name]
                ),
                "range": range_name,
            }

    def retail_health(self) -> list[dict[str, object]]:
        with ListingRepository(self.database_path) as repository:
            states = {row["retailer"]: row for row in repository.retail_provider_states()}
        return [states.get(name, {"retailer": name, "health": "experimental",
                                  "last_success_at": None, "last_failure_at": None,
                                  "next_refresh_at": None, "last_http_status": None,
                                  "last_error": None, "retrieval_method": None,
                                  "last_observation_at": None})
                for name in ("dns", "ozon", "wildberries")]

    def trigger_retail_refresh(self, comparable_key: str, query: str) -> bool:
        return self._trigger_retail_refresh(comparable_key, query, None)

    def _trigger_retail_refresh(self, comparable_key: str, query: str,
                                provider_names: set[str] | None) -> bool:
        if not self._retail_lock.acquire(blocking=False):
            return False
        threading.Thread(target=self._background_retail_refresh,
                         args=(comparable_key, query, provider_names),
                         name="resell-monitor-retail-refresh", daemon=True).start()
        return True

    def top_opportunities(self, *, limit: int = 8) -> list[dict[str, object]]:
        with ListingRepository(self.database_path) as repository:
            return repository.top_opportunities(limit=max(1, min(limit, 25)))

    def source_health(self) -> list[dict[str, object]]:
        searches = self._load_searches()
        with ListingRepository(self.database_path) as repository:
            states = repository.search_access_states()
        by_source: dict[str, list[SearchAccessState]] = {
            source: [] for source in ("avito", "farpost", "youla")
        }
        for state in states:
            by_source.setdefault(state.source, []).append(state)
        result: list[dict[str, object]] = []
        for source in ("avito", "farpost", "youla"):
            source_states = by_source[source]
            latest = max(
                source_states,
                key=lambda state: state.last_success_at
                or state.last_failure_at
                or datetime.min.replace(tzinfo=timezone.utc),
                default=None,
            )
            profile = next(
                (search for search in searches if search.source == source), None
            )
            health = (
                _worst_health(state.health for state in source_states)
                if source_states
                else "experimental"
                if source == "youla"
                else "degraded"
            )
            result.append(
                {
                    "source": source,
                    "health": health,
                    "last_success_at": latest.last_success_at if latest else None,
                    "last_failure_at": latest.last_failure_at if latest else None,
                    "last_http_status": latest.last_http_status if latest else None,
                    "transport": latest.last_transport if latest else None,
                    "consecutive_blocks": max(
                        (state.consecutive_blocks for state in source_states),
                        default=0,
                    ),
                    "blocked_until": max(
                        (
                            state.blocked_until
                            for state in source_states
                            if state.blocked_until
                        ),
                        default=None,
                    ),
                    "network_profile": self._network_profile(profile),
                    "searches": len(
                        [search for search in searches if search.source == source]
                    ),
                }
            )
        return result

    def dashboard(self) -> dict[str, object]:
        searches = self._load_searches()
        with ListingRepository(self.database_path) as repository:
            counts = repository.dashboard_counts()
            activity = repository.monitoring_events(limit=12)
        runtime = self.runtime()
        return {
            **counts,
            "active_searches": sum(search.enabled for search in searches),
            "source_health": self.source_health(),
            "recent_activity": activity,
            "last_scan_at": runtime["last_scan_at"],
            "next_scan": self._approximate_next_scan(searches),
            "runtime": runtime,
        }

    def settings(self) -> dict[str, object]:
        defaults: dict[str, object] = {
            "default_interval_seconds": 900,
            "default_jitter_seconds": 30,
            "default_block_retry_delay_seconds": 60,
            "default_block_cooldown_seconds": 900,
            "default_max_block_retries": 1,
            "default_export_format": "json",
            "ui_refresh_seconds": 2,
            "market_lookback_days": 30,
            "retail_refresh_interval_hours": 12,
            "retail_region": "Khabarovsk",
            "retail_dns_enabled": 0,
            "retail_ozon_enabled": 0,
            "retail_wildberries_enabled": 0,
        }
        with ListingRepository(self.database_path) as repository:
            stored = repository.app_settings()
        for key in defaults:
            if key in stored:
                defaults[key] = (
                    stored[key]
                    if key == "default_export_format"
                    else stored[key] if key == "retail_region" else int(stored[key])
                )
        return defaults

    def update_settings(self, payload: dict[str, object]) -> dict[str, object]:
        current = self.settings()
        values = {**current, **payload}
        numeric = (
            "default_interval_seconds",
            "default_jitter_seconds",
            "default_block_retry_delay_seconds",
            "default_block_cooldown_seconds",
            "default_max_block_retries",
            "ui_refresh_seconds",
            "market_lookback_days",
            "retail_refresh_interval_hours", "retail_dns_enabled",
            "retail_ozon_enabled", "retail_wildberries_enabled",
        )
        for key in numeric:
            values[key] = int(values[key])
        if values["default_interval_seconds"] < 60:
            raise ValueError("default interval must be at least 60 seconds")
        if not 0 <= values["default_jitter_seconds"] <= values["default_interval_seconds"] // 2:
            raise ValueError("default jitter must be between 0 and half the interval")
        if values["default_block_retry_delay_seconds"] < 0:
            raise ValueError("default retry delay must be non-negative")
        if values["default_block_cooldown_seconds"] < 60:
            raise ValueError("default cooldown must be at least 60 seconds")
        if values["default_max_block_retries"] not in {0, 1}:
            raise ValueError("default block retries must be 0 or 1")
        if not 1 <= values["ui_refresh_seconds"] <= 30:
            raise ValueError("UI refresh must be between 1 and 30 seconds")
        if not 7 <= values["market_lookback_days"] <= 365:
            raise ValueError("market lookback must be between 7 and 365 days")
        if not 1 <= values["retail_refresh_interval_hours"] <= 720:
            raise ValueError("retail refresh interval must be between 1 and 720 hours")
        values["retail_region"] = str(values.get("retail_region") or "").strip()
        for key in ("retail_dns_enabled", "retail_ozon_enabled", "retail_wildberries_enabled"):
            if values[key] not in {0, 1}:
                raise ValueError(f"{key} must be enabled or disabled")
        if values["default_export_format"] not in {"json", "txt", "html"}:
            raise ValueError("default export format must be json, txt, or html")
        with ListingRepository(self.database_path) as repository:
            repository.update_app_settings(values)
        return values

    def runtime(self) -> dict[str, object]:
        with self._state_lock:
            return {
                "monitoring": self._monitoring,
                "state": self._runtime_state,
                "last_scan_at": self._last_scan_at,
                "last_error": self._last_error,
            }

    def scan_now(self, search_name: str | None = None) -> list[SourceScan]:
        searches = [search for search in self._load_searches() if search.enabled]
        if search_name is not None:
            searches = [search for search in searches if search.name == search_name]
            if not searches:
                raise KeyError(search_name)
        if not searches:
            return []
        with self._scan_lock:
            try:
                scans = self._run_scans(searches)
                with self._state_lock:
                    self._last_scan_at = datetime.now(timezone.utc)
                    self._last_error = None
                    self._runtime_state = (
                        "Cooldown"
                        if any(scan.health.value == "cooldown" for scan in scans)
                        else "Monitoring"
                        if self._monitoring
                        else "Idle"
                    )
                return scans
            except Exception as error:
                with self._state_lock:
                    self._last_error = str(error)
                raise
            finally:
                with self._state_lock:
                    if self._runtime_state.startswith("Scanning"):
                        self._runtime_state = (
                            "Monitoring" if self._monitoring else "Idle"
                        )

    def trigger_scan(self, search_name: str | None = None) -> bool:
        if self._scan_lock.locked():
            return False
        thread = threading.Thread(
            target=self._background_scan,
            args=(search_name,),
            name="resell-monitor-manual-scan",
            daemon=True,
        )
        thread.start()
        return True

    def start_monitoring(self) -> bool:
        with self._state_lock:
            if self._monitoring:
                return False
            self._monitoring = True
            self._runtime_state = "Monitoring"
            self._stop_event.clear()
            self._monitor_thread = threading.Thread(
                target=self._monitor_loop,
                name="resell-monitor-scheduler",
                daemon=True,
            )
            self._monitor_thread.start()
            return True

    def stop_monitoring(self) -> bool:
        with self._state_lock:
            if not self._monitoring:
                return False
            self._monitoring = False
            self._runtime_state = "Idle"
            self._stop_event.set()
            thread = self._monitor_thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=5)
        return True

    def export(self, format_name: str) -> Path:
        exporters = {"json": export_json, "txt": export_txt, "html": export_html}
        try:
            exporter = exporters[format_name]
        except KeyError as error:
            raise ValueError(f"unsupported export format: {format_name}") from error
        path = self.output_dir / f"listings.{format_name}"
        with ListingRepository(self.database_path) as repository:
            exporter(repository.all(), path)
        return path

    def copy_for_analysis(
        self,
        *,
        selected: Iterable[tuple[str, str]] = (),
        include_new: bool = True,
        include_interesting: bool = True,
    ) -> str:
        selected_keys = set(selected)
        rows = self.listings()
        chosen = [
            row
            for row in rows
            if (row["source"], row["external_id"]) in selected_keys
            or include_new
            and row["status"] == "new"
            or include_interesting
            and row["status"] == "interesting"
        ]
        lines = ["Resell Monitor — listings for manual analysis"]
        for row in chosen:
            previous = row.get("previous_price")
            change = ""
            if previous is not None and row.get("current_price") is not None:
                change = f"; previous {previous} ₽"
            lines.append(
                f"[{row['source']}] {row['title']} — {row['price_display']}"
                f"{change}; {row.get('location') or 'location unknown'}; "
                f"status={row['status']}; score={row.get('ranking_score') or 0}; "
                f"{row['url']}"
            )
        return "\n".join(lines)

    def close(self) -> None:
        self.stop_monitoring()

    def _run_scans(self, searches: list[SearchConfig]) -> list[SourceScan]:
        runner = self._external_scan_runner or self._default_scan_runner
        return runner(searches, self._set_runtime_state)

    def _default_scan_runner(
        self, searches: list[SearchConfig], progress: Callable[[str], None]
    ) -> list[SourceScan]:
        sources = {
            (search.source, search.name): _build_source(search)
            for search in searches
        }
        scans: list[SourceScan] = []
        try:
            with ListingRepository(self.database_path) as repository:
                monitor = Monitor(sources, repository, debug_dir=self.debug_dir)
                for search in searches:
                    progress(f"Scanning {search.source.title()}…")
                    scans.extend(monitor.scan([search]))
        finally:
            for source in sources.values():
                try:
                    source.close()
                except Exception:
                    LOGGER.exception("Failed to close GUI scan source")
        return scans

    def _background_scan(self, search_name: str | None) -> None:
        try:
            self.scan_now(search_name)
        except Exception:
            LOGGER.exception("Manual GUI scan failed")

    def _background_retail_refresh(self, comparable_key: str, query: str,
                                   provider_names: set[str] | None = None) -> None:
        try:
            settings = self.settings()
            classes = {"dns": DnsRetailProvider, "ozon": OzonRetailProvider,
                       "wildberries": WildberriesRetailProvider}
            providers = {name: cls() for name, cls in classes.items()
                         if settings[f"retail_{name}_enabled"]
                         and (provider_names is None or name in provider_names)}
            if not providers:
                return
            try:
                with ListingRepository(self.database_path) as repository:
                    mappings = {str(row["retailer"]): str(row["product_url"])
                                for row in repository.retail_mappings(comparable_key)}
                    monitor = RetailMonitor(providers, repository,
                        interval=timedelta(hours=int(settings["retail_refresh_interval_hours"])))
                    monitor.refresh(comparable_key, query,
                                    region=str(settings["retail_region"]), mappings=mappings,
                                    progress=self._set_runtime_state)
            except Exception:
                LOGGER.exception("Manual retail refresh failed")
            finally:
                for provider in providers.values():
                    try: provider.close()
                    except Exception: LOGGER.exception("Failed to close retail provider")
                self._set_runtime_state("Monitoring" if self._monitoring else "Idle")
        finally:
            self._retail_lock.release()

    def _monitor_loop(self) -> None:
        while not self._stop_event.is_set():
            searches = self._load_searches()
            due = self._scheduler.due(searches)
            if due:
                try:
                    self.scan_now_for_scheduler(due)
                except Exception as error:
                    with self._state_lock:
                        self._last_error = str(error)
                    LOGGER.exception("Scheduled GUI scan failed")
                completed = time.monotonic()
                for search in due:
                    self._scheduler.mark_scanned(search, now=completed)
                continue
            self._start_due_retail_refresh()
            wait = self._scheduler.seconds_until_next(searches)
            self._stop_event.wait(1 if wait is None else min(max(wait, 0.1), 1))

    def _start_due_retail_refresh(self) -> bool:
        if self._retail_lock.locked():
            return False
        settings = self.settings()
        now = datetime.now(timezone.utc)
        with ListingRepository(self.database_path) as repository:
            states = {str(row["retailer"]): row for row in repository.retail_provider_states()}
            mappings = repository.retail_mappings()
        for mapping in mappings:
            name = str(mapping["retailer"])
            if not settings.get(f"retail_{name}_enabled"):
                continue
            next_value = states.get(name, {}).get("next_refresh_at")
            if next_value and datetime.fromisoformat(str(next_value)) > now:
                continue
            return self._trigger_retail_refresh(
                str(mapping["comparable_key"]), str(mapping["comparable_key"]), {name}
            )
        return False

    def scan_now_for_scheduler(self, searches: list[SearchConfig]) -> list[SourceScan]:
        with self._scan_lock:
            scans = self._run_scans(searches)
            with self._state_lock:
                self._last_scan_at = datetime.now(timezone.utc)
                self._last_error = None
                self._runtime_state = (
                    "Cooldown"
                    if any(scan.health.value == "cooldown" for scan in scans)
                    else "Monitoring"
                )
            return scans

    def _set_runtime_state(self, value: str) -> None:
        with self._state_lock:
            self._runtime_state = value

    def _load_searches(self) -> list[SearchConfig]:
        if not self.config_path.exists():
            return []
        return load_searches(self.config_path)

    @staticmethod
    def _search_index(searches: list[SearchConfig], name: str) -> int:
        for index, search in enumerate(searches):
            if search.name == name:
                return index
        raise KeyError(name)

    def _search_public(
        self,
        search: SearchConfig,
        *,
        state: SearchAccessState,
        include_secrets: bool = False,
    ) -> dict[str, object]:
        payload = asdict(search)
        proxy = payload.pop("proxy_url")
        payload["proxy_configured"] = bool(proxy)
        if include_secrets:
            payload["proxy_url"] = proxy
        payload.update(
            {
                "health": state.health,
                "last_result_count": state.last_result_count,
                "last_success_at": state.last_success_at,
                "last_status": state.last_http_status,
                "blocked_until": state.blocked_until,
                "next_scan": self._next_scan_for(search, state),
            }
        )
        return payload

    @staticmethod
    def _network_profile(search: SearchConfig | None) -> dict[str, object] | None:
        if search is None:
            return None
        return {
            "route": search.network_route,
            "proxy_configured": bool(search.proxy_url),
            "proxy_scheme": (
                search.proxy_url.split(":", 1)[0] if search.proxy_url else None
            ),
            "impersonation": search.avito_impersonation
            if search.source == "avito"
            else None,
            "session_mode": search.avito_session_mode
            if search.source == "avito"
            else None,
        }

    def _approximate_next_scan(self, searches: list[SearchConfig]) -> datetime | None:
        enabled = [search for search in searches if search.enabled]
        if not enabled:
            return None
        with ListingRepository(self.database_path) as repository:
            values = [
                self._next_scan_for(search, repository.search_access_state(search))
                for search in enabled
            ]
        return min(value for value in values if value is not None)

    @staticmethod
    def _next_scan_for(
        search: SearchConfig, state: SearchAccessState
    ) -> datetime | None:
        if not search.enabled:
            return None
        if state.blocked_until:
            return state.blocked_until
        anchor = state.last_success_at or state.last_failure_at
        if anchor is None:
            return datetime.now(timezone.utc)
        return anchor + timedelta(seconds=search.interval_seconds)


def _worst_health(values: Iterable[str]) -> str:
    order = {
        "failed": 6,
        "cooldown": 5,
        "temporarily-blocked": 4,
        "degraded": 3,
        "experimental": 2,
        "healthy": 1,
    }
    return max(values, key=lambda value: order.get(value, 0), default="experimental")


def _truthy(value: object) -> bool:
    return value is True or str(value).lower() in {"1", "true", "yes", "on"}


def _string_or_none(value: object) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def _integer_or_none(value: object) -> int | None:
    if value in (None, ""):
        return None
    return int(str(value))
