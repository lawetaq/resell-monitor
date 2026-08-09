from __future__ import annotations

import argparse
import logging
import time
from pathlib import Path

from src.config import load_searches
from src.models import SearchConfig
from src.monitor import Monitor
from src.reporting import export_html, export_json, export_txt
from src.scheduler import SearchScheduler
from src.sources.avito import AvitoSource
from src.sources.farpost import FarPostSource
from src.sources.youla import YoulaSource
from src.sources.http import HttpTransport
from src.storage import ListingRepository


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Resell Monitor backend")
    result.add_argument("--config", type=Path, required=True)
    result.add_argument("--database", type=Path, default=Path("data/resell-monitor.db"))
    result.add_argument("--debug-dir", type=Path)
    result.add_argument("--output-dir", type=Path, default=Path("output"))
    result.add_argument("--loop", action="store_true", help="run continuously using each search's own interval")
    return result


def run(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    searches = load_searches(args.config)
    args.database.parent.mkdir(parents=True, exist_ok=True)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    repository = ListingRepository(args.database)
    try:
        sources = {
            (search.source, search.name): _build_source(search)
            for search in searches
            if search.enabled
        }
    except ValueError as error:
        print(f"Invalid source configuration: {error}")
        repository.close()
        return 2
    monitor = Monitor(sources, repository, debug_dir=args.debug_dir)
    scheduler = SearchScheduler()
    try:
        while True:
            due = searches if not args.loop else scheduler.due(searches)
            if not due:
                wait = scheduler.seconds_until_next(searches)
                if wait is None:
                    print("No enabled searches.")
                    return 2
                time.sleep(min(wait, 60.0))
                continue
            scans = monitor.scan(due)
            if args.loop:
                completed_at = time.monotonic()
                for search in due:
                    scheduler.mark_scanned(search, now=completed_at)
            for scan in scans:
                state = scan.access_state
                health_detail = ""
                if state is not None:
                    health_detail = (
                        f"; last_success={state.last_success_at or 'never'}"
                        f"; last_status={state.last_http_status or 'unknown'}"
                        f"; blocked_until={state.blocked_until or 'none'}"
                    )
                print(
                    f"{scan.search.name}: {scan.health.value}; "
                    f"{len(scan.events)} listings"
                    + health_detail
                    + (f"; {scan.error}" if scan.error else "")
                )
            rows = repository.all()
            export_json(rows, args.output_dir / "listings.json")
            export_txt(rows, args.output_dir / "listings.txt")
            export_html(rows, args.output_dir / "listings.html")
            if not args.loop:
                return 0 if any(scan.health.value != "failed" for scan in scans) else 1
    except KeyboardInterrupt:
        return 130
    finally:
        for source in sources.values():
            try:
                source.close()
            except Exception:
                logging.exception("Failed to close source %s", source)
        repository.close()


def _build_source(search: SearchConfig):
    proxy = search.proxy_url if search.network_route == "proxy" else None
    if search.source == "avito":
        return AvitoSource(
            proxy=proxy,
            impersonation=search.avito_impersonation,
            session_mode=search.avito_session_mode,
        )
    if search.source == "farpost":
        return FarPostSource(HttpTransport(proxy=proxy))
    if search.source == "youla":
        return YoulaSource(HttpTransport(proxy=proxy))
    raise ValueError(f"unknown source: {search.source}")


if __name__ == "__main__":
    raise SystemExit(run())
