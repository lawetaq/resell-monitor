from __future__ import annotations

import sqlite3
import json
import hashlib
from statistics import median
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.analytics import (FreshnessPolicy, aggregate_market, assess_condition,
                           assess_resale, normalize_product,
                           unavailable_recommendation, validate_candidate)
from src.models import Listing, ListingAvailability, ListingStatus, SearchConfig
from src.retail import RetailPriceObservation


@dataclass(slots=True)
class UpsertOutcome:
    is_new: bool
    price_changed: bool
    previous_price: int | None = None


@dataclass(slots=True, frozen=True)
class SearchAccessState:
    source: str
    search_name: str
    search_url: str
    last_success_at: datetime | None = None
    last_failure_at: datetime | None = None
    consecutive_blocks: int = 0
    blocked_until: datetime | None = None
    last_http_status: int | None = None
    last_transport: str | None = None
    health: str = "experimental"
    last_error: str | None = None
    last_result_count: int = 0
    raw_items: int = 0
    valid_listings: int = 0
    rejected_items: int = 0
    priced_listings: int = 0


class ListingRepository:
    SCHEMA_VERSION = 8
    ANALYTICS_BACKFILL_VERSION = 3

    def __init__(self, path: str | Path, *, freshness_policy: FreshnessPolicy | None = None,
                 historical_comparable_days: int = 30) -> None:
        self.freshness_policy = freshness_policy or FreshnessPolicy()
        if historical_comparable_days < 1:
            raise ValueError("historical comparable window must be positive")
        self.historical_comparable_days = historical_comparable_days
        self.connection = sqlite3.connect(path, timeout=5.0)
        try:
            self.connection.row_factory = sqlite3.Row
            self.connection.execute("PRAGMA foreign_keys = ON")
            self.connection.execute("PRAGMA busy_timeout = 5000")
            self.migrate()
        except BaseException:
            try:
                self.connection.rollback()
            finally:
                self.connection.close()
            raise

    def migrate(self) -> None:
        version = int(self.connection.execute("PRAGMA user_version").fetchone()[0])
        if version > self.SCHEMA_VERSION:
            raise RuntimeError(
                f"database schema version {version} is newer than supported "
                f"version {self.SCHEMA_VERSION}"
            )
        if version == self.SCHEMA_VERSION:
            return
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            if version < 1:
                self._create_schema()
            if version < 2:
                self._migrate_analytics_v2()
            if version < 3:
                self._migrate_retail_v3()
            if version < 4:
                self._migrate_availability_v4()
            if version < 5:
                self._migrate_wildberries_v5()
            if version < 6:
                self._migrate_retail_browser_v6()
            if version < 7:
                self._migrate_quality_v7()
            if version < 8:
                self._migrate_normalization_v8()
            self.connection.execute(f"PRAGMA user_version = {self.SCHEMA_VERSION}")
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise

    def _create_schema(self) -> None:
        script = """
        CREATE TABLE IF NOT EXISTS listings (
          source TEXT NOT NULL, external_id TEXT NOT NULL, title TEXT NOT NULL,
          current_price INTEGER, price_display TEXT NOT NULL, location TEXT,
          url TEXT NOT NULL, description TEXT, first_seen TEXT NOT NULL,
          last_seen TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'new',
          PRIMARY KEY(source, external_id)
        );
        CREATE TABLE IF NOT EXISTS price_history (
          id INTEGER PRIMARY KEY, source TEXT NOT NULL, external_id TEXT NOT NULL,
          price INTEGER, observed_at TEXT NOT NULL,
          FOREIGN KEY(source, external_id) REFERENCES listings(source, external_id)
        );
        CREATE TABLE IF NOT EXISTS search_access_state (
          source TEXT NOT NULL, search_name TEXT NOT NULL, search_url TEXT NOT NULL,
          last_success_at TEXT, last_failure_at TEXT,
          consecutive_blocks INTEGER NOT NULL DEFAULT 0,
          blocked_until TEXT, last_http_status INTEGER, last_transport TEXT,
          health TEXT NOT NULL DEFAULT 'experimental', last_error TEXT,
          last_result_count INTEGER NOT NULL DEFAULT 0,
          PRIMARY KEY(source, search_name, search_url)
        );
        CREATE TABLE IF NOT EXISTS source_attempts (
          id INTEGER PRIMARY KEY,
          source TEXT NOT NULL, search_name TEXT NOT NULL, attempted_at TEXT NOT NULL,
          http_status INTEGER, transport TEXT, impersonation TEXT, session_mode TEXT,
          listing_count INTEGER, response_cookie_names TEXT NOT NULL DEFAULT '[]',
          block_classification TEXT NOT NULL DEFAULT 'none'
        );
        CREATE TABLE IF NOT EXISTS listing_searches (
          source TEXT NOT NULL, external_id TEXT NOT NULL, search_name TEXT NOT NULL,
          ranking_score REAL NOT NULL DEFAULT 0, last_matched_at TEXT NOT NULL,
          PRIMARY KEY(source, external_id, search_name),
          FOREIGN KEY(source, external_id) REFERENCES listings(source, external_id)
        );
        CREATE TABLE IF NOT EXISTS monitoring_events (
          id INTEGER PRIMARY KEY, occurred_at TEXT NOT NULL,
          event_type TEXT NOT NULL, source TEXT NOT NULL, external_id TEXT NOT NULL,
          search_name TEXT, old_price INTEGER, new_price INTEGER,
          FOREIGN KEY(source, external_id) REFERENCES listings(source, external_id)
        );
        CREATE TABLE IF NOT EXISTS app_settings (
          key TEXT PRIMARY KEY, value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS listing_observations (
          source TEXT NOT NULL, external_id TEXT NOT NULL, search_name TEXT NOT NULL,
          observed_at TEXT NOT NULL, price INTEGER,
          PRIMARY KEY(source, external_id, search_name, observed_at),
          FOREIGN KEY(source, external_id) REFERENCES listings(source, external_id)
        );
        CREATE TABLE IF NOT EXISTS search_scan_runs (
          search_name TEXT NOT NULL, source TEXT NOT NULL, observed_at TEXT NOT NULL,
          PRIMARY KEY(search_name, source, observed_at)
        );
        CREATE TABLE IF NOT EXISTS market_snapshots (
          comparable_key TEXT NOT NULL, snapshot_at TEXT NOT NULL,
          median REAL NOT NULL, q1 REAL NOT NULL, q3 REAL NOT NULL,
          sample_count INTEGER NOT NULL, new_listings_count INTEGER NOT NULL,
          price_drop_count INTEGER NOT NULL, disappeared_count INTEGER NOT NULL,
          minimum INTEGER, maximum INTEGER, recent_median REAL,
          PRIMARY KEY(comparable_key, snapshot_at)
        );
        CREATE TABLE IF NOT EXISTS retail_price_observations (
          id INTEGER PRIMARY KEY, comparable_key TEXT NOT NULL, retailer TEXT NOT NULL,
          price INTEGER NOT NULL, observed_at TEXT NOT NULL, url TEXT,
          UNIQUE(comparable_key, retailer, observed_at, price)
        );
        """
        for statement in script.split(";"):
            if statement.strip():
                self.connection.execute(statement)

    def _migrate_analytics_v2(self) -> None:
        self._ensure_column(
            "search_access_state", "health", "TEXT NOT NULL DEFAULT 'experimental'"
        )
        self._ensure_column("search_access_state", "last_error", "TEXT")
        self._ensure_column(
            "search_access_state", "last_result_count", "INTEGER NOT NULL DEFAULT 0"
        )
        for column, definition in (
            ("product_category", "TEXT"), ("manufacturer", "TEXT"),
            ("normalized_model", "TEXT"), ("variant", "TEXT"),
            ("capacity", "TEXT"), ("memory", "TEXT"),
            ("comparable_key", "TEXT"),
            ("match_confidence", "TEXT NOT NULL DEFAULT 'insufficient'"),
            ("condition_class", "TEXT NOT NULL DEFAULT 'OK'"),
            ("warning_phrases", "TEXT NOT NULL DEFAULT '[]'"),
            ("analytics_backfill_version", "INTEGER NOT NULL DEFAULT 0"),
        ):
            self._ensure_column("listings", column, definition)
        self._ensure_column("market_snapshots", "recent_median", "REAL")
        self._ensure_column("listing_searches", "target_price", "INTEGER")
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_listings_comparable ON listings(comparable_key)"
        )
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_observations_time ON listing_observations(observed_at)"
        )
        self._backfill_listing_analytics()

    def _migrate_retail_v3(self) -> None:
        for column, definition in (
            ("product_title", "TEXT"), ("normalized_model", "TEXT"),
            ("original_price", "INTEGER"), ("seller", "TEXT"),
            ("marketplace", "TEXT"),
            ("availability", "TEXT NOT NULL DEFAULT 'unknown'"),
            ("region", "TEXT"),
            ("match_confidence", "TEXT NOT NULL DEFAULT 'insufficient'"),
            ("delivery_price", "INTEGER"), ("offer_id", "TEXT"),
            ("product_id", "TEXT"), ("seller_kind", "TEXT"),
            ("fingerprint", "TEXT"), ("last_seen_at", "TEXT"),
        ):
            self._ensure_column("retail_price_observations", column, definition)
        self.connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_retail_fingerprint "
            "ON retail_price_observations(fingerprint) WHERE fingerprint IS NOT NULL"
        )
        self.connection.execute("""CREATE TABLE IF NOT EXISTS retail_provider_state (
            retailer TEXT PRIMARY KEY, health TEXT NOT NULL DEFAULT 'experimental',
            last_success_at TEXT, last_failure_at TEXT, last_http_status INTEGER,
            transport TEXT, last_error TEXT, next_refresh_at TEXT, region TEXT
        )""")
        self.connection.execute("""CREATE TABLE IF NOT EXISTS retail_product_mappings (
            comparable_key TEXT NOT NULL, retailer TEXT NOT NULL, product_url TEXT NOT NULL,
            product_id TEXT, enabled INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY(comparable_key, retailer)
        )""")

    def _migrate_availability_v4(self) -> None:
        self._ensure_column(
            "listings", "availability", "TEXT NOT NULL DEFAULT 'unknown'"
        )
        self._ensure_column("listings", "availability_updated_at", "TEXT")
        # Legacy rows with completed scan evidence can be classified. Rows without
        # an association stay UNKNOWN; recency alone is not proof of availability.
        rows = list(self.connection.execute(
            "SELECT source,external_id,last_seen FROM listings"
        ))
        now = datetime.now(timezone.utc)
        for row in rows:
            associations = list(self.connection.execute(
                """SELECT search_name,last_matched_at FROM listing_searches
                   WHERE source=? AND external_id=?""",
                (row["source"], row["external_id"]),
            ))
            if not associations:
                continue
            disappeared = all(self.connection.execute(
                """SELECT 1 FROM search_scan_runs WHERE source=? AND search_name=?
                   AND observed_at>? LIMIT 1""",
                (row["source"], association["search_name"], association["last_matched_at"]),
            ).fetchone() is not None for association in associations)
            last_seen = _parse_timestamp(row["last_seen"])
            availability = (
                ListingAvailability.DISAPPEARED if disappeared
                else ListingAvailability.ACTIVE
                if last_seen and now - _aware(last_seen) <= self.freshness_policy.active_for
                else ListingAvailability.STALE
            )
            self.connection.execute(
                """UPDATE listings SET availability=?,availability_updated_at=?
                   WHERE source=? AND external_id=?""",
                (availability.value, now.isoformat(), row["source"], row["external_id"]),
            )

    def _migrate_wildberries_v5(self) -> None:
        self._ensure_column("retail_price_observations", "conditional_price", "INTEGER")
        for column, definition in (
            ("retrieval_method", "TEXT"),
            ("last_observation_at", "TEXT"),
            ("block_classification", "TEXT NOT NULL DEFAULT 'none'"),
            ("retry_after_seconds", "INTEGER"),
            ("response_content_type", "TEXT"),
            ("response_size", "INTEGER"),
        ):
            self._ensure_column("retail_provider_state", column, definition)

    def _migrate_retail_browser_v6(self) -> None:
        self._ensure_column(
            "retail_price_observations", "retrieval_method",
            "TEXT NOT NULL DEFAULT 'http'",
        )
        self.connection.execute("""CREATE TABLE IF NOT EXISTS retail_provider_method_state (
            retailer TEXT NOT NULL, retrieval_method TEXT NOT NULL,
            health TEXT NOT NULL, last_success_at TEXT, last_failure_at TEXT,
            last_observation_at TEXT, last_error TEXT, region TEXT,
            PRIMARY KEY(retailer,retrieval_method)
        )""")

    def _migrate_quality_v7(self) -> None:
        for column, definition in (
            ("item_condition", "TEXT NOT NULL DEFAULT 'unknown'"),
            ("ram_type", "TEXT"), ("ram_module_type", "TEXT"), ("ecc", "INTEGER"),
            ("frequency_mhz", "INTEGER"), ("gpu_model", "TEXT"), ("vram_gb", "INTEGER"),
            ("cpu_model", "TEXT"), ("socket", "TEXT"), ("chipset", "TEXT"),
            ("ssd_capacity", "TEXT"), ("ssd_interface", "TEXT"),
            ("module_count", "INTEGER"), ("module_capacity_gb", "INTEGER"),
            ("total_capacity_gb", "INTEGER"),
            ("multi_item", "INTEGER NOT NULL DEFAULT 0"),
            ("price_ambiguous", "INTEGER NOT NULL DEFAULT 0"),
            ("needs_review", "INTEGER NOT NULL DEFAULT 0"),
            ("duplicate_group_id", "TEXT"),
            ("duplicate_probability", "REAL NOT NULL DEFAULT 0"),
            ("candidate_valid", "INTEGER NOT NULL DEFAULT 1"),
        ):
            self._ensure_column("listings", column, definition)
        for column, definition in (
            ("raw_items", "INTEGER NOT NULL DEFAULT 0"),
            ("valid_listings", "INTEGER NOT NULL DEFAULT 0"),
            ("rejected_items", "INTEGER NOT NULL DEFAULT 0"),
            ("priced_listings", "INTEGER NOT NULL DEFAULT 0"),
        ):
            self._ensure_column("search_access_state", column, definition)
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_listings_duplicate_group ON listings(duplicate_group_id)"
        )
        self.connection.execute("UPDATE listings SET analytics_backfill_version=1")
        keys = self._backfill_listing_analytics()
        now = datetime.now(timezone.utc).isoformat()
        for comparable_key in keys:
            if self.connection.execute(
                    "SELECT 1 FROM market_snapshots WHERE comparable_key=? LIMIT 1",
                    (comparable_key,)).fetchone() is None:
                self._create_market_snapshot(comparable_key, now)

    def _migrate_normalization_v8(self) -> None:
        for column, definition in (
            ("module_count", "INTEGER"), ("module_capacity_gb", "INTEGER"),
            ("total_capacity_gb", "INTEGER"),
        ):
            self._ensure_column("listings", column, definition)
        self._backfill_listing_analytics()

    def upsert(self, listing: Listing, *, observed_at: datetime | None = None) -> UpsertOutcome:
        timestamp = (observed_at or datetime.now(timezone.utc)).isoformat()
        with self.connection:
            return self._upsert(listing, timestamp)

    def upsert_many(
        self,
        listings: list[Listing],
        *,
        observed_at: datetime | None = None,
    ) -> list[UpsertOutcome]:
        """Persist one completed search result in a single transaction."""

        timestamp = (observed_at or datetime.now(timezone.utc)).isoformat()
        with self.connection:
            return [self._upsert(listing, timestamp) for listing in listings]

    def _upsert(self, listing: Listing, timestamp: str) -> UpsertOutcome:
        product = normalize_product(listing.title, listing.description)
        condition = assess_condition(listing.title, listing.description)
        row = self.connection.execute("SELECT current_price FROM listings WHERE source=? AND external_id=?", (listing.source, listing.external_id)).fetchone()
        if row is None:
            self.connection.execute(
                """INSERT INTO listings(
                       source, external_id, title, current_price, price_display,
                       location, url, description, first_seen, last_seen, status,
                       product_category, manufacturer, normalized_model, variant,
                       capacity, memory, comparable_key, match_confidence,
                       condition_class, warning_phrases, analytics_backfill_version,
                       availability, availability_updated_at
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (listing.source, listing.external_id, listing.title, listing.price, listing.price_display, listing.location, listing.url, listing.description, timestamp, timestamp, ListingStatus.NEW.value,
                 product.product_category, product.manufacturer, product.normalized_model,
                 product.variant, product.capacity, product.memory, product.comparable_key,
                 product.match_confidence, condition.condition_class,
                 json.dumps(condition.matched_warning_phrases, ensure_ascii=False),
                 self.ANALYTICS_BACKFILL_VERSION, listing.availability.value, timestamp),
            )
            self.connection.execute("INSERT INTO price_history(source,external_id,price,observed_at) VALUES (?,?,?,?)", (listing.source, listing.external_id, listing.price, timestamp))
            self._update_quality_fields(listing, product, condition)
            return UpsertOutcome(True, False)
        previous = row["current_price"]
        changed = previous != listing.price
        availability = (ListingAvailability.ARCHIVED if listing.availability is ListingAvailability.ARCHIVED
                        else ListingAvailability.ACTIVE)
        self.connection.execute("""UPDATE listings SET title=?,current_price=?,price_display=?,location=?,url=?,description=?,last_seen=?,availability=?,availability_updated_at=?,
            product_category=?,manufacturer=?,normalized_model=?,variant=?,capacity=?,memory=?,comparable_key=?,match_confidence=?,condition_class=?,warning_phrases=?,analytics_backfill_version=?
            WHERE source=? AND external_id=?""", (listing.title, listing.price, listing.price_display, listing.location, listing.url, listing.description, timestamp, availability.value, timestamp,
            product.product_category, product.manufacturer, product.normalized_model, product.variant,
            product.capacity, product.memory, product.comparable_key, product.match_confidence,
            condition.condition_class, json.dumps(condition.matched_warning_phrases, ensure_ascii=False),
            self.ANALYTICS_BACKFILL_VERSION,
            listing.source, listing.external_id))
        if changed:
            self.connection.execute("INSERT INTO price_history(source,external_id,price,observed_at) VALUES (?,?,?,?)", (listing.source, listing.external_id, listing.price, timestamp))
        self._update_quality_fields(listing, product, condition)
        return UpsertOutcome(False, changed, previous)

    def _update_quality_fields(self, listing: Listing, product: object,
                               condition: object) -> None:
        normalized_title = " ".join(listing.title.casefold().split())
        group_material = "\x1f".join((listing.source, normalized_title,
                                      str(getattr(product, "normalized_model", "") or ""),
                                      str(listing.price or ""),
                                      (listing.location or "").casefold()))
        group_id = hashlib.sha256(group_material.encode()).hexdigest()[:16]
        multi = bool(getattr(product, "multi_item", False))
        ambiguous = bool(getattr(product, "price_ambiguous", False))
        item_condition = str(getattr(condition, "item_condition", "unknown"))
        needs_review = multi or ambiguous or item_condition in {"faulty", "parts_only"}
        candidate_valid = not (listing.source == "youla" and not validate_candidate(listing).valid)
        self.connection.execute(
            """UPDATE listings SET item_condition=?,ram_type=?,ram_module_type=?,ecc=?,
               frequency_mhz=?,gpu_model=?,vram_gb=?,cpu_model=?,socket=?,chipset=?,
               ssd_capacity=?,ssd_interface=?,module_count=?,module_capacity_gb=?,
               total_capacity_gb=?,multi_item=?,price_ambiguous=?,needs_review=?,
               duplicate_group_id=?,duplicate_probability=?,candidate_valid=?
               WHERE source=? AND external_id=?""",
            (item_condition, getattr(product, "ram_type", None),
             getattr(product, "ram_module_type", None), getattr(product, "ecc", None),
             getattr(product, "frequency_mhz", None), getattr(product, "gpu_model", None),
             getattr(product, "vram_gb", None), getattr(product, "cpu_model", None),
             getattr(product, "socket", None), getattr(product, "chipset", None),
             getattr(product, "ssd_capacity", None), getattr(product, "ssd_interface", None),
             getattr(product, "module_count", None), getattr(product, "module_capacity_gb", None),
             getattr(product, "total_capacity_gb", None),
             int(multi), int(ambiguous), int(needs_review), group_id, 0.0, int(candidate_valid),
             listing.source, listing.external_id),
        )
        duplicates = self.connection.execute(
            "SELECT COUNT(*) FROM listings WHERE duplicate_group_id=?", (group_id,)
        ).fetchone()[0]
        if duplicates > 1:
            self.connection.execute(
                "UPDATE listings SET duplicate_probability=.95 WHERE duplicate_group_id=?",
                (group_id,),
            )

    def set_status(self, source: str, external_id: str, status: ListingStatus) -> None:
        cursor = self.connection.execute("UPDATE listings SET status=? WHERE source=? AND external_id=?", (status.value, source, external_id))
        if cursor.rowcount != 1:
            raise KeyError((source, external_id))
        self.connection.commit()

    def all(self) -> list[sqlite3.Row]:
        return list(self.connection.execute("SELECT * FROM listings ORDER BY first_seen DESC"))

    def history(self, source: str, external_id: str) -> list[sqlite3.Row]:
        return list(self.connection.execute("SELECT * FROM price_history WHERE source=? AND external_id=? ORDER BY observed_at", (source, external_id)))

    def search_access_state(self, search: SearchConfig) -> SearchAccessState:
        row = self.connection.execute(
            """SELECT * FROM search_access_state
               WHERE source=? AND search_name=? AND search_url=?""",
            (search.source, search.name, search.url),
        ).fetchone()
        if row is None:
            return SearchAccessState(search.source, search.name, search.url)
        return SearchAccessState(
            source=row["source"],
            search_name=row["search_name"],
            search_url=row["search_url"],
            last_success_at=_parse_timestamp(row["last_success_at"]),
            last_failure_at=_parse_timestamp(row["last_failure_at"]),
            consecutive_blocks=row["consecutive_blocks"],
            blocked_until=_parse_timestamp(row["blocked_until"]),
            last_http_status=row["last_http_status"],
            last_transport=row["last_transport"],
            health=row["health"],
            last_error=row["last_error"],
            last_result_count=row["last_result_count"],
            raw_items=row["raw_items"], valid_listings=row["valid_listings"],
            rejected_items=row["rejected_items"], priced_listings=row["priced_listings"],
        )

    def record_search_success(
        self,
        search: SearchConfig,
        *,
        status: int | None,
        transport: str | None,
        observed_at: datetime,
        health: str = "healthy",
        result_count: int = 0,
        raw_items: int = 0, valid_listings: int = 0,
        rejected_items: int = 0, priced_listings: int = 0,
        error: str | None = None,
    ) -> SearchAccessState:
        self._ensure_search_state(search)
        with self.connection:
            self.connection.execute(
                """UPDATE search_access_state
                   SET last_success_at=?, consecutive_blocks=0, blocked_until=NULL,
                       last_http_status=?, last_transport=?, health=?, last_error=?,
                       last_result_count=?,raw_items=?,valid_listings=?,
                       rejected_items=?,priced_listings=?
                   WHERE source=? AND search_name=? AND search_url=?""",
                (
                    observed_at.isoformat(),
                    status,
                    transport,
                    health,
                    error,
                    result_count,
                    raw_items, valid_listings, rejected_items, priced_listings,
                    search.source,
                    search.name,
                    search.url,
                ),
            )
        return self.search_access_state(search)

    def record_search_failure(
        self,
        search: SearchConfig,
        *,
        status: int | None,
        transport: str | None,
        observed_at: datetime,
        error: str | None = None,
    ) -> SearchAccessState:
        self._ensure_search_state(search)
        with self.connection:
            self.connection.execute(
                """UPDATE search_access_state
                   SET last_failure_at=?, last_http_status=?, last_transport=?,
                       health='failed', last_error=?
                   WHERE source=? AND search_name=? AND search_url=?""",
                (
                    observed_at.isoformat(),
                    status,
                    transport,
                    error,
                    search.source,
                    search.name,
                    search.url,
                ),
            )
        return self.search_access_state(search)

    def record_search_block(
        self,
        search: SearchConfig,
        *,
        status: int,
        transport: str | None,
        observed_at: datetime,
        enter_cooldown: bool,
    ) -> SearchAccessState:
        self._ensure_search_state(search)
        blocked_until = (
            observed_at + timedelta(seconds=search.block_cooldown_seconds)
            if enter_cooldown
            else None
        )
        with self.connection:
            self.connection.execute(
                """UPDATE search_access_state
                   SET last_failure_at=?,
                       consecutive_blocks=consecutive_blocks + 1,
                       blocked_until=?, last_http_status=?, last_transport=?,
                       health=?, last_error=?
                   WHERE source=? AND search_name=? AND search_url=?""",
                (
                    observed_at.isoformat(),
                    blocked_until.isoformat() if blocked_until else None,
                    status,
                    transport,
                    "cooldown" if enter_cooldown else "temporarily-blocked",
                    f"HTTP {status}",
                    search.source,
                    search.name,
                    search.url,
                ),
            )
        return self.search_access_state(search)

    def record_source_attempt(
        self,
        search: SearchConfig,
        *,
        attempted_at: datetime,
        status: int | None,
        transport: str | None,
        impersonation: str | None,
        session_mode: str | None,
        listing_count: int | None,
        response_cookie_names: tuple[str, ...],
        block_classification: str,
    ) -> None:
        with self.connection:
            self.connection.execute(
                """INSERT INTO source_attempts(
                       source, search_name, attempted_at, http_status, transport,
                       impersonation, session_mode, listing_count,
                       response_cookie_names, block_classification
                   ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    search.source,
                    search.name,
                    attempted_at.isoformat(),
                    status,
                    transport,
                    impersonation,
                    session_mode,
                    listing_count,
                    json.dumps(sorted(set(response_cookie_names))),
                    block_classification,
                ),
            )

    def source_attempts(self, source: str, search_name: str) -> list[sqlite3.Row]:
        return list(
            self.connection.execute(
                """SELECT * FROM source_attempts
                   WHERE source=? AND search_name=? ORDER BY id""",
                (source, search_name),
            )
        )

    def record_scan_metadata(
        self,
        search: SearchConfig,
        listings: list[Listing],
        outcomes: list[UpsertOutcome],
        scores: list[float],
        *,
        observed_at: datetime | None = None,
    ) -> None:
        timestamp = (observed_at or datetime.now(timezone.utc)).isoformat()
        with self.connection:
            self.connection.execute(
                "INSERT OR IGNORE INTO search_scan_runs(search_name,source,observed_at) VALUES (?,?,?)",
                (search.name, search.source, timestamp),
            )
            for listing, outcome, score in zip(
                listings, outcomes, scores, strict=True
            ):
                self.connection.execute(
                    """INSERT INTO listing_searches(
                           source, external_id, search_name, ranking_score,
                           last_matched_at, target_price
                       ) VALUES (?,?,?,?,?,?)
                       ON CONFLICT(source, external_id, search_name) DO UPDATE SET
                         ranking_score=excluded.ranking_score,
                         last_matched_at=excluded.last_matched_at,
                         target_price=excluded.target_price""",
                    (
                        listing.source,
                        listing.external_id,
                        search.name,
                        score,
                        timestamp,
                        search.target_price,
                    ),
                )
                event_type: str | None = None
                if outcome.is_new:
                    event_type = "new"
                elif outcome.price_changed:
                    event_type = (
                        "price_drop"
                        if outcome.previous_price is not None
                        and listing.price is not None
                        and listing.price < outcome.previous_price
                        else "price_change"
                    )
                self.connection.execute(
                    """INSERT OR IGNORE INTO listing_observations(
                           source, external_id, search_name, observed_at, price
                       ) VALUES (?,?,?,?,?)""",
                    (listing.source, listing.external_id, search.name, timestamp, listing.price),
                )
                if event_type:
                    self.connection.execute(
                        """INSERT INTO monitoring_events(
                               occurred_at, event_type, source, external_id,
                               search_name, old_price, new_price
                           ) VALUES (?,?,?,?,?,?,?)""",
                        (
                            timestamp,
                            event_type,
                            listing.source,
                            listing.external_id,
                            search.name,
                            outcome.previous_price,
                            listing.price,
                        ),
                    )
                availability = (
                    ListingAvailability.ARCHIVED
                    if listing.availability is ListingAvailability.ARCHIVED
                    else ListingAvailability.ACTIVE
                )
                self.connection.execute(
                    """UPDATE listings SET availability=?,availability_updated_at=?
                       WHERE source=? AND external_id=?""",
                    (availability.value, timestamp, listing.source, listing.external_id),
                )
            self._mark_scan_disappearances(search, timestamp)
            keys = {normalize_product(item.title, item.description).comparable_key for item in listings}
            lookback = int(self.app_settings().get("market_lookback_days", "30"))
            for key in keys - {None}:
                self._create_market_snapshot(str(key), timestamp, lookback)

    def _mark_scan_disappearances(self, search: SearchConfig, timestamp: str) -> None:
        candidates = self.connection.execute(
            """SELECT l.source,l.external_id,l.availability
               FROM listings l JOIN listing_searches ls USING(source,external_id)
               WHERE ls.source=? AND ls.search_name=? AND ls.last_matched_at<?
                 AND l.availability!='archived'""",
            (search.source, search.name, timestamp),
        )
        for row in candidates:
            # A listing remains active if any other associated search still has no
            # completed scan after its last match.
            still_observed = False
            for association in self.connection.execute(
                """SELECT search_name,last_matched_at FROM listing_searches
                   WHERE source=? AND external_id=?""",
                (row["source"], row["external_id"]),
            ):
                later = self.connection.execute(
                    """SELECT 1 FROM search_scan_runs WHERE source=? AND search_name=?
                       AND observed_at>? LIMIT 1""",
                    (row["source"], association["search_name"], association["last_matched_at"]),
                ).fetchone()
                if later is None:
                    still_observed = True
                    break
            if not still_observed:
                self.connection.execute(
                    """UPDATE listings SET availability='disappeared',availability_updated_at=?
                       WHERE source=? AND external_id=?""",
                    (timestamp, row["source"], row["external_id"]),
                )

    def create_market_snapshot(
        self, comparable_key: str, *, observed_at: datetime | None = None,
        lookback_days: int = 30,
    ) -> dict[str, object] | None:
        timestamp = (observed_at or datetime.now(timezone.utc)).isoformat()
        with self.connection:
            return self._create_market_snapshot(comparable_key, timestamp, lookback_days)

    def _create_market_snapshot(
        self, comparable_key: str, timestamp: str, lookback_days: int = 30
    ) -> dict[str, object] | None:
        at = _parse_timestamp(timestamp)
        assert at is not None
        cutoff = (at - timedelta(days=lookback_days)).isoformat()
        raw_rows = list(self.connection.execute(
            """SELECT lo.source,lo.external_id,lo.search_name,lo.price,lo.observed_at latest,
                      l.duplicate_group_id
               FROM listing_observations lo JOIN listings l USING(source,external_id)
               WHERE l.comparable_key=? AND lo.observed_at BETWEEN ? AND ? AND lo.price IS NOT NULL
                 AND l.candidate_valid=1 AND l.condition_class!='FAULT'
                 AND l.multi_item=0 AND l.price_ambiguous=0
               ORDER BY lo.observed_at""",
            (comparable_key, cutoff, timestamp),
        ))
        collapsed: dict[tuple[str, str], dict[str, object]] = {}
        for row in raw_rows:
            identity = (str(row["source"]), str(row["external_id"]))
            item = collapsed.setdefault(identity, {
                "source": row["source"], "external_id": row["external_id"],
                "price": row["price"], "latest": row["latest"],
                "duplicate_group_id": row["duplicate_group_id"], "search_names": set(),
            })
            item["search_names"].add(str(row["search_name"]))  # type: ignore[union-attr]
            if str(row["latest"]) >= str(item["latest"]):
                item["price"], item["latest"] = row["price"], row["latest"]
        rows: list[dict[str, object]] = list(collapsed.values())
        if not rows:
            # Legacy/local test data may predate observation tracking.
            rows = [dict(row) for row in self.connection.execute(
                """SELECT source,external_id,'' search_name,current_price price,last_seen latest,
                          duplicate_group_id
                   FROM listings WHERE comparable_key=? AND current_price IS NOT NULL
                   AND candidate_valid=1 AND condition_class!='FAULT'
                   AND multi_item=0 AND price_ambiguous=0
                   AND last_seen BETWEEN ? AND ?""", (comparable_key, cutoff, timestamp)
            )]
        deduplicated: dict[str, dict[str, object]] = {}
        for row in rows:
            group = str(row.get("duplicate_group_id") or f"{row['source']}:{row['external_id']}")
            deduplicated.setdefault(group, row)
        rows = list(deduplicated.values())
        prices = [int(row["price"]) for row in rows]
        recent_cutoff = (at - timedelta(days=7)).isoformat()
        recent_prices = [int(row["price"]) for row in rows if row["latest"] >= recent_cutoff]
        new_count = self.connection.execute(
            """SELECT COUNT(DISTINCT me.source || ':' || me.external_id)
               FROM monitoring_events me JOIN listings l USING(source,external_id)
               WHERE l.comparable_key=? AND me.event_type='new' AND me.occurred_at BETWEEN ? AND ?""",
            (comparable_key, cutoff, timestamp),
        ).fetchone()[0]
        drop_count = self.connection.execute(
            """SELECT COUNT(*) FROM monitoring_events me JOIN listings l USING(source,external_id)
               WHERE l.comparable_key=? AND me.event_type='price_drop' AND me.occurred_at BETWEEN ? AND ?""",
            (comparable_key, cutoff, timestamp),
        ).fetchone()[0]
        disappeared_keys: set[tuple[str, str]] = set()
        for row in rows:
            names = row.get("search_names") or ({str(row["search_name"])} if row.get("search_name") else set())
            for search_name in names:
                later_run = self.connection.execute(
                    """SELECT 1 FROM search_scan_runs WHERE search_name=? AND source=?
                       AND observed_at>? AND observed_at<=? LIMIT 1""",
                    (search_name, row["source"], row["latest"], timestamp),
                ).fetchone()
                if later_run is not None:
                    disappeared_keys.add((str(row["source"]), str(row["external_id"])))
                    break
        snapshot = aggregate_market(comparable_key, prices, at, new_count=new_count,
                                    drop_count=drop_count,
                                    disappeared_count=len(disappeared_keys),
                                    recent_prices=recent_prices)
        if snapshot is None:
            return None
        self.connection.execute(
            """INSERT OR REPLACE INTO market_snapshots(
               comparable_key,snapshot_at,median,q1,q3,sample_count,new_listings_count,
               price_drop_count,disappeared_count,minimum,maximum,recent_median) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (snapshot.comparable_key, timestamp, snapshot.median, snapshot.q1, snapshot.q3,
             snapshot.sample_count, snapshot.new_listings_count, snapshot.price_drop_count,
             snapshot.disappeared_count, snapshot.minimum, snapshot.maximum,
             snapshot.recent_median),
        )
        return dict(self.connection.execute(
            "SELECT * FROM market_snapshots WHERE comparable_key=? AND snapshot_at=?",
            (comparable_key, timestamp),
        ).fetchone())

    def market_snapshots(self, comparable_key: str, *, days: int | None = 30,
                         now: datetime | None = None) -> list[dict[str, object]]:
        parameters: list[object] = [comparable_key]
        clause = ""
        if days is not None:
            cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=days)
            clause = " AND snapshot_at>=?"
            parameters.append(cutoff.isoformat())
        return [dict(row) for row in self.connection.execute(
            f"SELECT * FROM market_snapshots WHERE comparable_key=?{clause} ORDER BY snapshot_at",
            parameters,
        )]

    def market_search(
        self, query: str = "", *, product_category: str | None = None
    ) -> list[dict[str, object]]:
        if product_category not in {None, "gpu", "cpu", "ram", "ssd", "motherboard"}:
            raise ValueError("unsupported product category")
        pattern = f"%{query.casefold()}%"
        rows = self.connection.execute(
            """SELECT comparable_key,product_category,normalized_model,variant,
                      COUNT(*) listing_count,MAX(last_seen) last_updated
               FROM listings WHERE comparable_key IS NOT NULL AND candidate_valid=1
                 AND (? IS NULL OR product_category=?)
                 AND (?='' OR LOWER(normalized_model || ' ' || COALESCE(variant,'') || ' ' || comparable_key) LIKE ?)
               GROUP BY comparable_key ORDER BY last_updated DESC""",
            (product_category, product_category, query, pattern),
        )
        result = [dict(row) for row in rows]
        for row in result:
            row["display_label"] = _product_display_label(row)
        return result

    def market_summary(self, comparable_key: str) -> dict[str, object] | None:
        snapshots = self.market_snapshots(comparable_key, days=None)
        if not snapshots:
            return None
        current = snapshots[-1]
        rows = [row for row in self.listing_rows() if row.get("comparable_key") == comparable_key]
        current_rows = [row for row in rows if row.get("current_price") is not None
                        and bool(row.get("is_available"))]
        current_rows.sort(key=lambda row: int(row["current_price"]))
        trend_7 = _snapshot_trend(snapshots, 7)
        trend_30 = _snapshot_trend(snapshots, 30)
        assessments = [self._assessment(row, current, trend_7, trend_30) for row in current_rows]
        strongest = max(zip(current_rows, assessments, strict=True), key=lambda pair: pair[1]["score"] or -1, default=None)
        product = self.connection.execute(
            "SELECT product_category,normalized_model,variant FROM listings WHERE comparable_key=? LIMIT 1",
            (comparable_key,),
        ).fetchone()
        return {**current, **(dict(product) if product else {}),
                "trend_7d_percent": trend_7, "trend_30d_percent": trend_30,
                "market_trend": _trend_text(trend_7 if trend_7 is not None else trend_30),
                "confidence": _market_confidence(int(current["sample_count"])),
                "historical_observation_count": int(current["sample_count"]),
                "active_listing_count": len(current_rows),
                "current_market_state": ("active" if current_rows else
                                         "no_active_listings"),
                "cheapest_listing": current_rows[0] if current_rows else None,
                "strongest_candidate": ({**strongest[0], **strongest[1]} if strongest else None),
                "last_updated": current["snapshot_at"]}

    def _assessment(self, row: dict[str, object], snapshot: dict[str, object],
                    trend_7: float | None, trend_30: float | None) -> dict[str, object]:
        target_row = self.connection.execute(
            """SELECT MAX(target_price) target_price FROM listing_searches
               WHERE source=? AND external_id=?""",
            (row["source"], row["external_id"]),
        ).fetchone()
        retail = self.retail_summary(str(row.get("comparable_key"))) if row.get("comparable_key") else None
        retail_price = int(retail["representative_price"]) if retail and retail.get("representative_price") else None
        candidate_market, broadened = self._candidate_market(row)
        market = candidate_market or {
            "median": None, "q1": None, "q3": None, "minimum": None, "sample_count": 0,
        }
        source_degraded = self.connection.execute(
            """SELECT 1 FROM listing_searches ls JOIN search_access_state ss
               ON ss.source=ls.source AND ss.search_name=ls.search_name
               WHERE ls.source=? AND ls.external_id=? AND ss.health='degraded' LIMIT 1""",
            (row["source"], row["external_id"]),
        ).fetchone() is not None
        result = assess_resale(
            asking_price=row.get("current_price"),
            median=float(market["median"]) if market.get("median") is not None else None,
            q1=float(market["q1"]) if market.get("q1") is not None else None,
            sample_count=int(market["sample_count"]),
            first_seen=_parse_timestamp(str(row["first_seen"])), trend_7d=trend_7,
            trend_30d=trend_30,
            activity_count=(int(snapshot["new_listings_count"])
                            + int(snapshot["price_drop_count"])
                            + int(snapshot["disappeared_count"])),
            condition_class=str(row.get("condition_class") or "OK"),
            target_price=target_row["target_price"] if target_row else None,
            retail_price=retail_price,
            retail_confidence=str(retail.get("confidence", "insufficient")) if retail else "insufficient",
            match_confidence=str(row.get("match_confidence") or "insufficient"),
            multi_item=bool(row.get("multi_item")), price_ambiguous=bool(row.get("price_ambiguous")),
            ram_compatibility_unknown=(row.get("product_category") == "ram" and
                                       (row.get("ram_module_type") == "unknown" or row.get("ecc") is None)),
            source_degraded=source_degraded, geography_broadened=False,
            item_condition=str(row.get("item_condition") or "unknown"),
            liquidity_fallback=_liquidity_fallback(row),
            comparable_fallback=str(market.get("comparable_tier") or "exact") != "exact",
            historical_fallback_used=bool(market.get("historical_fallback_used")),
        )
        return {
            "score": result.score, "recommendation": result.recommendation,
            "market_discount_percent": result.market_discount_percent,
            "estimated_resale_price": result.estimated_resale_price,
            "expected_gross_margin": result.expected_gross_margin,
            "confidence": result.confidence, "market_trend": result.market_trend,
            "market_median": market["median"], "median_competitor_price": market["median"],
            "market_q1": market["q1"], "market_q3": market["q3"],
            "low_market_price": market.get("minimum"), "market_sample_count": market["sample_count"],
            "sample_size": market["sample_count"], "geography_broadened": broadened,
            "active_sample_size": market.get("active_sample_size", market["sample_count"]),
            "historical_sample_size": market.get("historical_sample_size", 0),
            "comparable_tier": market.get("comparable_tier", "exact"),
            "historical_fallback_used": bool(market.get("historical_fallback_used")),
            "retail_reference_price": retail_price,
            "listing_discount_vs_retail": result.listing_discount_vs_retail,
            "used_new_gap_percent": result.used_new_gap_percent,
            "overall_score": result.overall_score, "deal_score": result.deal_score,
            "confidence_score": result.confidence_score,
            "liquidity_score": result.liquidity_score, "risk_score": result.risk_score,
            "priority": result.priority, "verdict": result.verdict,
            "raw_score_band": result.raw_score_band,
            "needs_review": result.needs_review or bool(row.get("needs_review")),
            "requires_review": result.requires_review or bool(row.get("needs_review")),
            "score_reasons": list(result.score_reasons), "risk_reasons": list(result.risk_reasons),
            "review_reasons": list(result.review_reasons),
            "target_buy_price": result.target_buy_price, "max_buy_price": result.max_buy_price,
            "expected_gross_profit": result.expected_gross_margin,
        }

    def _candidate_market(self, candidate: dict[str, object]) -> tuple[dict[str, object] | None, bool]:
        all_rows = [dict(row) for row in self.connection.execute(
            """SELECT source,external_id,current_price,location,duplicate_group_id,
                      comparable_key,product_category,normalized_model,vram_gb,
                      availability,last_seen
               FROM listings WHERE current_price IS NOT NULL
                 AND NOT (source=? AND external_id=?) AND candidate_valid=1
                 AND condition_class!='FAULT' AND multi_item=0 AND price_ambiguous=0""",
            (candidate.get("source"), candidate.get("external_id")),
        )]
        rows = [item for item in all_rows
                if item.get("comparable_key") == candidate.get("comparable_key")]
        tier = "exact"
        active_exact = [item for item in rows if item.get("availability") == "active"]
        if candidate.get("product_category") == "gpu" and len(active_exact) < 3:
            model_rows = [item for item in all_rows
                          if item.get("product_category") == "gpu"
                          and item.get("normalized_model") == candidate.get("normalized_model")]
            known_variants = {int(item["vram_gb"]) for item in model_rows
                              if item.get("vram_gb") is not None}
            candidate_vram = candidate.get("vram_gb")
            compatible = (len(known_variants) <= 1 if candidate_vram is None else
                          all(value == int(candidate_vram) for value in known_variants))
            if compatible:
                rows = model_rows
                tier = "model_vram_relaxed"
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(days=self.historical_comparable_days)
        rows = [item for item in rows if item.get("availability") == "active" or
                (_parse_timestamp(str(item.get("last_seen") or "")) is not None and
                 _aware(_parse_timestamp(str(item["last_seen"])) or now) >= cutoff)]
        unique: dict[str, dict[str, object]] = {}
        for item in rows:
            unique.setdefault(str(item.get("duplicate_group_id") or
                                  f"{item['source']}:{item['external_id']}"), item)
        rows = list(unique.values())
        chosen, broadened = rows, False
        active = [item for item in chosen if item.get("availability") == "active"]
        historical = [item for item in chosen if item.get("availability") != "active"]
        historical_used = len(active) == 2 and bool(historical)
        market_rows = active if len(active) >= 3 else active + historical if historical_used else []
        if len(market_rows) < 3:
            return None, broadened
        snapshot = aggregate_market(str(candidate.get("comparable_key")),
                                    [int(item["current_price"]) for item in market_rows],
                                    datetime.now(timezone.utc))
        result = asdict(snapshot) if snapshot else None
        if result is not None:
            result.update({"active_sample_size": len(active),
                           "historical_sample_size": len(historical),
                           "historical_fallback_used": historical_used,
                           "comparable_tier": tier})
        return result, broadened

    def top_opportunities(self, *, limit: int = 10) -> list[dict[str, object]]:
        opportunities: list[dict[str, object]] = []
        for product in self.market_search():
            summary = self.market_summary(str(product["comparable_key"]))
            if summary and summary["strongest_candidate"]:
                candidate = dict(summary["strongest_candidate"])
                if candidate.get("score") is not None and candidate.get("priority") != "reject" \
                        and not candidate.get("needs_review"):
                    opportunities.append(candidate)
        return sorted(opportunities, key=lambda row: int(row["score"]), reverse=True)[:limit]

    def update_dynamic_scores(self, search: SearchConfig, listings: list[Listing]) -> list[float]:
        scores: list[float] = []
        with self.connection:
            for listing in listings:
                detail = self.listing_detail(listing.source, listing.external_id)
                score = float(detail["score"] or 0)
                scores.append(score)
                self.connection.execute(
                    """UPDATE listing_searches SET ranking_score=?
                       WHERE source=? AND external_id=? AND search_name=?""",
                    (score, listing.source, listing.external_id, search.name),
                )
        return scores

    def _is_current_listing(self, row: dict[str, object]) -> bool:
        associations = self.connection.execute(
            """SELECT search_name,last_matched_at FROM listing_searches
               WHERE source=? AND external_id=?""",
            (row["source"], row["external_id"]),
        )
        found = False
        for association in associations:
            found = True
            later = self.connection.execute(
                """SELECT 1 FROM search_scan_runs WHERE source=? AND search_name=?
                   AND observed_at>? LIMIT 1""",
                (row["source"], association["search_name"], association["last_matched_at"]),
            ).fetchone()
            if later is None:
                return True
        return not found

    def add_retail_observation(self, observation: RetailPriceObservation) -> None:
        identity = observation.offer_id or observation.product_id or observation.url or observation.product_title or "unknown"
        fingerprint = hashlib.sha256("\x1f".join((
            observation.comparable_key, observation.retailer, identity,
            str(observation.price), observation.region or "",
            observation.availability, observation.retrieval_method,
        )).encode()).hexdigest()
        timestamp = observation.observed_at.isoformat()
        with self.connection:
            cursor = self.connection.execute(
                """INSERT OR IGNORE INTO retail_price_observations(
                   comparable_key,retailer,price,observed_at,url,product_title,
                   normalized_model,original_price,seller,marketplace,availability,
                   region,match_confidence,delivery_price,offer_id,product_id,
                   seller_kind,fingerprint,last_seen_at,retrieval_method)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (observation.comparable_key, observation.retailer, observation.price,
                 timestamp, observation.url, observation.product_title,
                 observation.normalized_model, observation.original_price,
                 observation.seller, observation.marketplace, observation.availability,
                 observation.region, observation.match_confidence,
                 observation.delivery_price, observation.offer_id,
                 observation.product_id, observation.seller_kind, fingerprint, timestamp,
                 observation.retrieval_method),
            )
            if cursor.rowcount == 0:
                self.connection.execute(
                    "UPDATE retail_price_observations SET last_seen_at=? WHERE fingerprint=?",
                    (timestamp, fingerprint),
                )
            if observation.conditional_price is not None:
                self.connection.execute(
                    "UPDATE retail_price_observations SET conditional_price=? WHERE fingerprint=?",
                    (observation.conditional_price, fingerprint),
                )

    def retail_observations(self, comparable_key: str) -> list[dict[str, object]]:
        return [dict(row) for row in self.connection.execute(
            "SELECT * FROM retail_price_observations WHERE comparable_key=? ORDER BY observed_at",
            (comparable_key,),
        )]

    def latest_retail_observation(
        self, comparable_key: str, retailer: str,
        *, retrieval_method: str | None = None,
    ) -> dict[str, object] | None:
        clause = " AND retrieval_method=?" if retrieval_method else ""
        parameters: tuple[object, ...] = (
            (comparable_key, retailer, retrieval_method)
            if retrieval_method else (comparable_key, retailer)
        )
        row = self.connection.execute(
            "SELECT * FROM retail_price_observations WHERE comparable_key=? "
            f"AND retailer=?{clause} ORDER BY observed_at DESC,id DESC LIMIT 1",
            parameters,
        ).fetchone()
        return dict(row) if row else None

    def retail_summary(self, comparable_key: str, *, stale_hours: int = 48,
                       now: datetime | None = None) -> dict[str, object] | None:
        rows = self.retail_observations(comparable_key)
        if not rows:
            return None
        current: dict[tuple[str, str], dict[str, object]] = {}
        for row in rows:
            identity = str(row.get("offer_id") or row.get("product_id") or row.get("url") or row["id"])
            current[(str(row["retailer"]), identity)] = row
        usable = [row for row in current.values() if row["availability"] not in {"unavailable", "out_of_stock"}
                  and row["match_confidence"] in {"exact", "high"} and int(row["price"]) > 0]
        if not usable:
            return {"representative_price": None, "cheapest_price": None,
                    "retailer_count": 0, "offer_count": 0, "confidence": "insufficient",
                    "last_updated": max(str(row["last_seen_at"] or row["observed_at"]) for row in rows),
                    "retailers": []}
        cutoff = (now or datetime.now(timezone.utc)) - timedelta(hours=stale_hours)
        breakdown = []
        for retailer in sorted({str(row["retailer"]) for row in usable}):
            offers = [row for row in usable if row["retailer"] == retailer]
            prices = [int(row["price"]) for row in offers]
            updated = max(str(row["last_seen_at"] or row["observed_at"]) for row in offers)
            breakdown.append({"retailer": retailer, "representative_price": int(round(median(prices))),
                              "cheapest_price": min(prices), "offer_count": len(prices),
                              "last_updated": updated,
                              "stale": (_parse_timestamp(updated) or cutoff) < cutoff,
                              "availability": "available"})
        representative_prices = [int(row["representative_price"]) for row in breakdown]
        count = len(usable)
        return {"representative_price": int(round(median(representative_prices))),
                "cheapest_price": min(int(row["price"]) for row in usable),
                "retailer_count": len(breakdown), "offer_count": count,
                "confidence": "high" if len(breakdown) >= 2 and count >= 4 else "medium" if count >= 2 else "low",
                "last_updated": max(str(row["last_seen_at"] or row["observed_at"]) for row in usable),
                "retailers": breakdown}

    def retail_history(self, comparable_key: str, *, days: int | None = 30,
                       now: datetime | None = None) -> list[dict[str, object]]:
        cutoff = None if days is None else (now or datetime.now(timezone.utc)) - timedelta(days=days)
        groups: dict[tuple[str, str], list[dict[str, object]]] = {}
        for row in self.retail_observations(comparable_key):
            timestamp = _parse_timestamp(str(row["observed_at"]))
            if cutoff and timestamp and timestamp < cutoff:
                continue
            bucket = str(row["observed_at"])[:13]
            groups.setdefault((str(row["retailer"]), bucket), []).append(row)
        result = []
        for (retailer, _), rows in groups.items():
            prices = [int(row["price"]) for row in rows if int(row["price"]) > 0]
            if prices:
                result.append({"observed_at": min(str(row["observed_at"]) for row in rows),
                               "retailer": retailer, "representative_price": int(round(median(prices))),
                               "cheapest_price": min(prices), "offer_count": len(prices),
                               "availability": "available" if any(row["availability"] == "available" for row in rows) else str(rows[0]["availability"])})
        return sorted(result, key=lambda row: (str(row["observed_at"]), str(row["retailer"])))

    def record_retail_provider_state(self, retailer: str, *, health: str,
                                     successful: bool, status: int | None,
                                     transport: str, error: str | None,
                                     observed_at: datetime, next_refresh_at: datetime | None,
                                     region: str | None,
                                     retrieval_method: str | None = None,
                                     last_observation_at: datetime | None = None,
                                     block_classification: str = "none",
                                     retry_after_seconds: int | None = None,
                                     response_content_type: str | None = None,
                                     response_size: int | None = None) -> None:
        success = observed_at.isoformat() if successful else None
        failure = None if successful else observed_at.isoformat()
        with self.connection:
            self.connection.execute("""INSERT INTO retail_provider_state(
                retailer,health,last_success_at,last_failure_at,last_http_status,
                transport,last_error,next_refresh_at,region,retrieval_method,
                last_observation_at,block_classification,retry_after_seconds,
                response_content_type,response_size) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(retailer) DO UPDATE SET health=excluded.health,
                last_success_at=COALESCE(excluded.last_success_at,retail_provider_state.last_success_at),
                last_failure_at=COALESCE(excluded.last_failure_at,retail_provider_state.last_failure_at),
                last_http_status=excluded.last_http_status,transport=excluded.transport,
                last_error=excluded.last_error,next_refresh_at=excluded.next_refresh_at,
                region=excluded.region,retrieval_method=excluded.retrieval_method,
                last_observation_at=COALESCE(excluded.last_observation_at,retail_provider_state.last_observation_at),
                block_classification=excluded.block_classification,
                retry_after_seconds=excluded.retry_after_seconds,
                response_content_type=excluded.response_content_type,
                response_size=excluded.response_size""", (retailer, health, success, failure, status,
                transport, error, next_refresh_at.isoformat() if next_refresh_at else None, region,
                retrieval_method, last_observation_at.isoformat() if last_observation_at else None,
                block_classification, retry_after_seconds, response_content_type, response_size))

    def retail_provider_states(self) -> list[dict[str, object]]:
        return [dict(row) for row in self.connection.execute(
            "SELECT * FROM retail_provider_state ORDER BY retailer")]

    def record_retail_method_state(
        self, retailer: str, retrieval_method: str, *, health: str,
        successful: bool, observed_at: datetime, error: str | None,
        region: str | None, has_observation: bool,
    ) -> None:
        success = observed_at.isoformat() if successful else None
        failure = None if successful else observed_at.isoformat()
        observation = observed_at.isoformat() if has_observation else None
        with self.connection:
            self.connection.execute("""INSERT INTO retail_provider_method_state(
                retailer,retrieval_method,health,last_success_at,last_failure_at,
                last_observation_at,last_error,region) VALUES (?,?,?,?,?,?,?,?)
                ON CONFLICT(retailer,retrieval_method) DO UPDATE SET
                health=excluded.health,
                last_success_at=COALESCE(excluded.last_success_at,retail_provider_method_state.last_success_at),
                last_failure_at=COALESCE(excluded.last_failure_at,retail_provider_method_state.last_failure_at),
                last_observation_at=COALESCE(excluded.last_observation_at,retail_provider_method_state.last_observation_at),
                last_error=excluded.last_error,region=excluded.region""",
                (retailer, retrieval_method, health, success, failure, observation,
                 error, region),
            )

    def retail_provider_method_states(self) -> list[dict[str, object]]:
        return [dict(row) for row in self.connection.execute(
            "SELECT * FROM retail_provider_method_state ORDER BY retailer,retrieval_method"
        )]

    def set_retail_mapping(self, comparable_key: str, retailer: str,
                           product_url: str, product_id: str | None = None) -> None:
        with self.connection:
            self.connection.execute("""INSERT INTO retail_product_mappings(
                comparable_key,retailer,product_url,product_id) VALUES (?,?,?,?)
                ON CONFLICT(comparable_key,retailer) DO UPDATE SET
                product_url=excluded.product_url,product_id=excluded.product_id,enabled=1""",
                (comparable_key, retailer, product_url, product_id))

    def retail_mappings(self, comparable_key: str | None = None) -> list[dict[str, object]]:
        if comparable_key:
            rows = self.connection.execute("SELECT * FROM retail_product_mappings WHERE comparable_key=? AND enabled=1", (comparable_key,))
        else:
            rows = self.connection.execute("SELECT * FROM retail_product_mappings WHERE enabled=1")
        return [dict(row) for row in rows]

    def delete_retail_mapping(self, comparable_key: str, retailer: str) -> None:
        with self.connection:
            self.connection.execute(
                "DELETE FROM retail_product_mappings WHERE comparable_key=? AND retailer=?",
                (comparable_key, retailer),
            )

    def listing_rows(
        self,
        *,
        source: str | None = None,
        search_name: str | None = None,
        status: str | None = None,
        new_only: bool = False,
        price_drops: bool = False,
        min_price: int | None = None,
        max_price: int | None = None,
        text: str | None = None,
        availability: str | None = None,
        location: str | None = None,
        sort_by: str | None = None,
        sort_direction: str | None = None,
    ) -> list[dict[str, object]]:
        clauses: list[str] = ["l.candidate_valid=1"]
        parameters: list[object] = []
        if source:
            clauses.append("l.source=?")
            parameters.append(source)
        if search_name:
            clauses.append(
                "EXISTS (SELECT 1 FROM listing_searches sx WHERE "
                "sx.source=l.source AND sx.external_id=l.external_id "
                "AND sx.search_name=?)"
            )
            parameters.append(search_name)
        if status:
            clauses.append("l.status=?")
            parameters.append(status)
        if new_only:
            clauses.append("l.status='new'")
        if min_price is not None:
            clauses.append("l.current_price>=?")
            parameters.append(min_price)
        if max_price is not None:
            clauses.append("l.current_price<=?")
            parameters.append(max_price)
        if text:
            clauses.append("LOWER(l.title || ' ' || COALESCE(l.description,'')) LIKE ?")
            parameters.append(f"%{text.casefold()}%")
        if availability:
            if availability == "unavailable":
                clauses.append("l.availability IN ('archived','disappeared')")
            elif availability == "unknown":
                clauses.append("l.availability=?")
            parameters.append(availability)
        if location:
            clauses.append("LOWER(COALESCE(l.location, '')) = LOWER(?)")
            parameters.append(location)
        if price_drops:
            clauses.append(
                "EXISTS (SELECT 1 FROM monitoring_events me WHERE "
                "me.source=l.source AND me.external_id=l.external_id "
                "AND me.event_type='price_drop')"
            )
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.connection.execute(
            f"""SELECT l.*,
                 (SELECT ph.price FROM price_history ph
                  WHERE ph.source=l.source AND ph.external_id=l.external_id
                  ORDER BY ph.id DESC LIMIT 1 OFFSET 1) AS previous_price,
                 (SELECT MAX(ls.ranking_score) FROM listing_searches ls
                  WHERE ls.source=l.source AND ls.external_id=l.external_id) AS ranking_score,
                 (SELECT GROUP_CONCAT(ls.search_name, ', ') FROM listing_searches ls
                  WHERE ls.source=l.source AND ls.external_id=l.external_id) AS search_names
               FROM listings l {where}
               ORDER BY l.last_seen DESC""",
            parameters,
        )
        result = [dict(row) for row in rows]
        for item in result:
            warnings = item.get("warning_phrases") or "[]"
            item["matched_warning_phrases"] = json.loads(str(warnings))
            key = item.get("comparable_key")
            if not key:
                item.update(_insufficient_assessment(item))
                self._apply_availability(item)
                continue
            snapshots = self.market_snapshots(str(key), days=None)
            if not snapshots:
                item.update(_insufficient_assessment(item))
                self._apply_availability(item)
                continue
            snapshot = snapshots[-1]
            item.update(self._assessment(
                item, snapshot, _snapshot_trend(snapshots, 7),
                _snapshot_trend(snapshots, 30),
            ))
            item["ranking_score"] = item["score"]
            self._apply_availability(item)
        if availability in {"active", "stale", "unknown"}:
            result = [row for row in result if row.get("availability") == availability]
        if sort_by is not None:
            fields = {"price": "current_price", "date": "first_seen", "rating": "overall_score"}
            if sort_by not in fields or sort_direction not in {"asc", "desc"}:
                raise ValueError("unsupported listing sort")
            field = fields[sort_by]
            present = [row for row in result if row.get(field) is not None]
            missing = [row for row in result if row.get(field) is None]
            present.sort(key=lambda row: row[field], reverse=sort_direction == "desc")
            result = present + missing
        return result

    def _apply_availability(self, item: dict[str, object]) -> None:
        try:
            stored = ListingAvailability(str(item.get("availability") or "unknown"))
        except ValueError:
            stored = ListingAvailability.UNKNOWN
        effective = self.freshness_policy.effective_availability(
            stored,
            last_seen=_parse_timestamp(str(item["last_seen"])) if item.get("last_seen") else None,
        )
        available = effective is ListingAvailability.ACTIVE
        current = self.freshness_policy.is_actionable(effective)
        actionable = (current and item.get("priority") in {"1", "2", "3"}
                      and item.get("verdict") not in {"PASS", "REJECT", "NEEDS REVIEW"}
                      and item.get("condition_class") != "FAULT")
        item["availability"] = effective.value
        item["is_available"] = available
        item["is_current"] = current
        item["is_actionable"] = actionable
        item["freshness_seconds"] = _age_seconds(item.get("last_seen"))
        if not current:
            item["non_actionable_reason"] = unavailable_recommendation(effective)
            item["recommendation"] = item["non_actionable_reason"]
            item["verdict"] = item["non_actionable_reason"]
            item["actionable_score"] = None
        elif not actionable:
            item["non_actionable_reason"] = str(item.get("verdict") or "NOT ACTIONABLE")
            item["actionable_score"] = None
        else:
            item["non_actionable_reason"] = None
            item["actionable_score"] = item.get("score")

    def listing_detail(self, source: str, external_id: str) -> dict[str, object]:
        rows = self.listing_rows(source=source)
        listing = next(
            (row for row in rows if row["external_id"] == external_id), None
        )
        if listing is None:
            raise KeyError((source, external_id))
        listing["price_history"] = [
            dict(row) for row in self.history(source, external_id)
        ]
        return listing

    def monitoring_events(self, *, limit: int = 100) -> list[dict[str, object]]:
        rows = self.connection.execute(
            """SELECT me.*, l.title, l.url, l.price_display
               FROM monitoring_events me
               JOIN listings l USING(source, external_id)
               ORDER BY me.id DESC LIMIT ?""",
            (limit,),
        )
        return [dict(row) for row in rows]

    def search_access_states(self) -> list[SearchAccessState]:
        rows = self.connection.execute(
            "SELECT * FROM search_access_state ORDER BY source, search_name"
        )
        return [
            SearchAccessState(
                row["source"],
                row["search_name"],
                row["search_url"],
                _parse_timestamp(row["last_success_at"]),
                _parse_timestamp(row["last_failure_at"]),
                row["consecutive_blocks"],
                _parse_timestamp(row["blocked_until"]),
                row["last_http_status"],
                row["last_transport"],
                row["health"],
                row["last_error"],
                row["last_result_count"],
                row["raw_items"], row["valid_listings"],
                row["rejected_items"], row["priced_listings"],
            )
            for row in rows
        ]

    def dashboard_counts(self) -> dict[str, int]:
        row = self.connection.execute(
            """SELECT
                 SUM(CASE WHEN status='new' THEN 1 ELSE 0 END) AS new_count,
                 SUM(CASE WHEN status='interesting' THEN 1 ELSE 0 END) AS interesting_count
               FROM listings"""
        ).fetchone()
        drops = self.connection.execute(
            "SELECT COUNT(*) FROM monitoring_events WHERE event_type='price_drop'"
        ).fetchone()[0]
        return {
            "new": int(row["new_count"] or 0),
            "interesting": int(row["interesting_count"] or 0),
            "price_drops": int(drops),
        }

    def app_settings(self) -> dict[str, str]:
        return {
            row["key"]: row["value"]
            for row in self.connection.execute("SELECT key, value FROM app_settings")
        }

    def update_app_settings(self, values: dict[str, object]) -> None:
        with self.connection:
            self.connection.executemany(
                """INSERT INTO app_settings(key, value) VALUES (?,?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                [(key, str(value)) for key, value in values.items()],
            )

    def _ensure_search_state(self, search: SearchConfig) -> None:
        with self.connection:
            self.connection.execute(
                """INSERT OR IGNORE INTO search_access_state(
                       source, search_name, search_url
                   ) VALUES (?,?,?)""",
                (search.source, search.name, search.url),
            )

    def _ensure_column(self, table: str, column: str, definition: str) -> None:
        columns = {
            row["name"] for row in self.connection.execute(f"PRAGMA table_info({table})")
        }
        if column not in columns:
            self.connection.execute(
                f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
            )

    def _backfill_listing_analytics(self) -> set[str]:
        rows = self.connection.execute(
            """SELECT * FROM listings
               WHERE analytics_backfill_version<?""",
            (self.ANALYTICS_BACKFILL_VERSION,),
        ).fetchall()
        keys: set[str] = set()
        for row in rows:
            product = normalize_product(row["title"], row["description"])
            condition = assess_condition(row["title"], row["description"])
            self.connection.execute(
                """UPDATE listings SET product_category=?,manufacturer=?,normalized_model=?,
                   variant=?,capacity=?,memory=?,comparable_key=?,match_confidence=?,
                   condition_class=?,warning_phrases=?,analytics_backfill_version=?
                   WHERE source=? AND external_id=?""",
                (product.product_category, product.manufacturer, product.normalized_model,
                 product.variant, product.capacity, product.memory, product.comparable_key,
                 product.match_confidence, condition.condition_class,
                 json.dumps(condition.matched_warning_phrases, ensure_ascii=False),
                 self.ANALYTICS_BACKFILL_VERSION,
                 row["source"], row["external_id"]),
            )
            columns = {column["name"] for column in self.connection.execute("PRAGMA table_info(listings)")}
            if "item_condition" in columns:
                listing = Listing(
                    str(row["source"]), str(row["external_id"]), str(row["title"]),
                    row["current_price"], str(row["price_display"]), row["location"],
                    str(row["url"]), row["description"],
                )
                self._update_quality_fields(listing, product, condition)
            if product.comparable_key:
                keys.add(product.comparable_key)
        return keys

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> ListingRepository:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def _parse_timestamp(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _age_seconds(value: object) -> int | None:
    timestamp = _parse_timestamp(str(value)) if value else None
    if timestamp is None:
        return None
    return max(0, int((datetime.now(timezone.utc) - _aware(timestamp)).total_seconds()))


def _snapshot_trend(snapshots: list[dict[str, object]], days: int) -> float | None:
    if len(snapshots) < 2:
        return None
    latest_time = _parse_timestamp(str(snapshots[-1]["snapshot_at"]))
    assert latest_time is not None
    cutoff = latest_time - timedelta(days=days)
    eligible = [row for row in snapshots[:-1]
                if (_parse_timestamp(str(row["snapshot_at"])) or latest_time) >= cutoff]
    baseline = eligible[0] if eligible else None
    if baseline is None or not float(baseline["median"]):
        return None
    return round((float(snapshots[-1]["median"]) - float(baseline["median"])) /
                 float(baseline["median"]) * 100, 1)


def _trend_text(value: float | None) -> str:
    if value is None or abs(value) < 3:
        return "STABLE"
    return "RISING" if value > 0 else "FALLING"


def _market_confidence(count: int) -> str:
    return "high" if count >= 10 else "medium" if count >= 5 else "low" if count >= 3 else "insufficient"


def _liquidity_fallback(row: dict[str, object]) -> int:
    model = str(row.get("normalized_model") or "").casefold()
    category = row.get("product_category")
    if row.get("ram_module_type") in {"RDIMM", "LRDIMM"}:
        return 25
    if category == "ram" and row.get("capacity") in {"8GB", "16GB"}:
        return 70
    if category == "ssd" and row.get("capacity") in {"500GB", "512GB", "1TB"}:
        return 68
    if any(segment in model for segment in ("ryzen 5", "i3 ", "i5 ", "i7 ",
                                             "rtx 3060", "rtx 3060 ti", "rtx 3070", "rx 6600")):
        return 75
    if category in {"gpu", "cpu", "ram", "ssd", "motherboard"}:
        return 50
    return 35


def _insufficient_assessment(row: dict[str, object] | None = None) -> dict[str, object]:
    row = row or {}
    assessment = assess_resale(
        asking_price=row.get("current_price"), median=None, q1=None, sample_count=0,
        first_seen=None, condition_class=str(row.get("condition_class") or "OK"),
        multi_item=bool(row.get("multi_item")),
        price_ambiguous=bool(row.get("price_ambiguous")),
        item_condition=str(row.get("item_condition") or "unknown"),
    )
    return {"score": None, "recommendation": assessment.recommendation,
            "market_discount_percent": None, "estimated_resale_price": None,
            "expected_gross_margin": None, "confidence": "insufficient",
            "market_trend": "STABLE", "market_median": None,
            "market_q1": None, "market_q3": None, "market_sample_count": 0,
            "median_competitor_price": None, "low_market_price": None, "sample_size": 0,
            "overall_score": None, "deal_score": None, "confidence_score": 0,
            "liquidity_score": 0, "risk_score": assessment.risk_score,
            "priority": assessment.priority,
            "verdict": assessment.verdict, "needs_review": True,
            "requires_review": True,
            "score_reasons": [], "risk_reasons": list(assessment.risk_reasons),
            "review_reasons": list(assessment.review_reasons),
            "target_buy_price": None, "max_buy_price": None,
            "expected_gross_profit": None,
            "raw_score_band": assessment.raw_score_band}


def _product_display_label(product: dict[str, object]) -> str:
    model = str(product.get("normalized_model") or "Unknown model")
    variant = str(product.get("variant") or "").strip()
    category = product.get("product_category")
    if category == "gpu" and variant == "memory-unknown":
        return f"{model} · VRAM unknown"
    if not variant:
        return model
    return f"{model} · {variant.replace(' ', ' · ')}"
