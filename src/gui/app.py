from __future__ import annotations

import argparse
import json
import mimetypes
import threading
import webbrowser
from dataclasses import asdict, is_dataclass
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from src.gui.service import GuiService

STATIC_DIR = Path(__file__).with_name("static")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Resell Monitor GUI")
    parser.add_argument("--config", type=Path, default=Path("searches.json"))
    parser.add_argument("--database", type=Path, default=Path("data/resell-monitor.db"))
    parser.add_argument("--output-dir", type=Path, default=Path("output"))
    parser.add_argument("--debug-dir", type=Path)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    return parser


def create_handler(service: GuiService) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "ResellMonitor/0.1"

        def do_GET(self) -> None:
            try:
                self._get()
            except Exception as error:
                self._error(error)

        def do_POST(self) -> None:
            try:
                self._post()
            except Exception as error:
                self._error(error)

        def do_PUT(self) -> None:
            try:
                name = self._path_after("/api/searches/")
                self._json(HTTPStatus.OK, service.update_search(name, self._body()))
            except Exception as error:
                self._error(error)

        def do_PATCH(self) -> None:
            try:
                parts = self._path_parts()
                if len(parts) == 4 and parts[:2] == ["api", "listings"]:
                    payload = self._body()
                    self._json(
                        HTTPStatus.OK,
                        service.set_listing_status(parts[2], parts[3], str(payload["status"])),
                    )
                    return
                self.send_error(HTTPStatus.NOT_FOUND)
            except Exception as error:
                self._error(error)

        def do_DELETE(self) -> None:
            try:
                name = self._path_after("/api/searches/")
                service.delete_search(name)
                self._json(HTTPStatus.OK, {"deleted": name})
            except Exception as error:
                self._error(error)

        def log_message(self, format: str, *args: object) -> None:
            return

        def _get(self) -> None:
            parsed = urlsplit(self.path)
            query = {key: values[-1] for key, values in parse_qs(parsed.query).items()}
            if parsed.path == "/api/dashboard":
                self._json(HTTPStatus.OK, service.dashboard())
            elif parsed.path == "/api/searches":
                self._json(HTTPStatus.OK, service.searches())
            elif parsed.path == "/api/listings":
                self._json(HTTPStatus.OK, service.listings(query))
            elif parsed.path.startswith("/api/listings/"):
                parts = self._path_parts()
                if len(parts) != 4:
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                self._json(HTTPStatus.OK, service.listing_detail(parts[2], parts[3]))
            elif parsed.path == "/api/history":
                self._json(HTTPStatus.OK, service.history())
            elif parsed.path == "/api/sources":
                self._json(HTTPStatus.OK, service.source_health())
            elif parsed.path == "/api/runtime":
                self._json(HTTPStatus.OK, service.runtime())
            elif parsed.path == "/api/settings":
                self._json(HTTPStatus.OK, service.settings())
            elif parsed.path == "/api/retail/health":
                self._json(HTTPStatus.OK, service.retail_health())
            elif parsed.path == "/api/retail/browser":
                self._json(HTTPStatus.OK, service.retail_browser_status())
            elif parsed.path == "/api/retail/mappings":
                key = query.get("comparable_key", "")
                if not key:
                    raise ValueError("comparable_key is required")
                self._json(HTTPStatus.OK, service.retail_mappings(key))
            elif parsed.path == "/api/market":
                self._json(
                    HTTPStatus.OK,
                    service.market_search(
                        query.get("q", ""), query.get("category") or None
                    ),
                )
            elif parsed.path == "/api/market/opportunities":
                self._json(HTTPStatus.OK, service.top_opportunities())
            elif parsed.path.startswith("/api/market/"):
                key = self._path_after("/api/market/")
                self._json(
                    HTTPStatus.OK,
                    service.market_product(key, query.get("range", "30D")),
                )
            else:
                self._static(parsed.path)

        def _post(self) -> None:
            path = urlsplit(self.path).path
            if path == "/api/searches":
                self._json(HTTPStatus.CREATED, service.create_search(self._body()))
            elif path.endswith("/toggle") and path.startswith("/api/searches/"):
                name = unquote(path[len("/api/searches/") : -len("/toggle")].strip("/"))
                payload = self._body()
                self._json(
                    HTTPStatus.OK,
                    service.set_search_enabled(name, bool(payload["enabled"])),
                )
            elif path.endswith("/run") and path.startswith("/api/searches/"):
                name = unquote(path[len("/api/searches/") : -len("/run")].strip("/"))
                self._json(HTTPStatus.ACCEPTED, {"started": service.trigger_scan(name)})
            elif path == "/api/scan":
                self._json(HTTPStatus.ACCEPTED, {"started": service.trigger_scan()})
            elif path == "/api/monitor/start":
                self._json(HTTPStatus.OK, {"started": service.start_monitoring()})
            elif path == "/api/monitor/stop":
                self._json(HTTPStatus.OK, {"stopped": service.stop_monitoring()})
            elif path == "/api/export":
                export_path = service.export(str(self._body()["format"]))
                self._json(HTTPStatus.OK, {"path": str(export_path)})
            elif path == "/api/copy":
                payload = self._body()
                selected = [tuple(item) for item in payload.get("selected", [])]
                self._json(
                    HTTPStatus.OK,
                    {
                        "text": service.copy_for_analysis(
                            selected=selected,
                            include_new=bool(payload.get("include_new", True)),
                            include_interesting=bool(
                                payload.get("include_interesting", True)
                            ),
                        )
                    },
                )
            elif path == "/api/settings":
                self._json(HTTPStatus.OK, service.update_settings(self._body()))
            elif path == "/api/retail/refresh":
                payload = self._body()
                self._json(HTTPStatus.ACCEPTED, {"started": service.trigger_retail_refresh(
                    str(payload["comparable_key"]), str(payload.get("query") or payload["comparable_key"]))})
            elif path == "/api/retail/browser/open":
                self._json(HTTPStatus.OK, service.open_retail_browser())
            elif path == "/api/retail/browser/engine":
                payload = self._body()
                self._json(HTTPStatus.OK, service.set_retail_browser_engine(
                    str(payload["engine"])))
            elif path == "/api/retail/browser/close":
                self._json(HTTPStatus.OK, service.close_retail_browser())
            elif path == "/api/retail/browser/reset":
                self._json(HTTPStatus.OK, service.reset_retail_browser_profile(
                    confirmed=bool(self._body().get("confirmed"))))
            elif path == "/api/retail/mapping":
                payload = self._body()
                self._json(HTTPStatus.OK, service.save_retail_mapping(
                    str(payload["comparable_key"]), str(payload["retailer"]),
                    str(payload["product_url"])))
            elif path == "/api/retail/mapping/delete":
                payload = self._body()
                service.delete_retail_mapping(str(payload["comparable_key"]),
                                              str(payload["retailer"]))
                self._json(HTTPStatus.OK, {"deleted": True})
            elif path == "/api/retail/browser/navigate":
                payload = self._body()
                self._json(HTTPStatus.OK, service.open_retail_mapping(
                    str(payload["comparable_key"]), str(payload["retailer"])))
            elif path == "/api/retail/browser/capture":
                payload = self._body()
                self._json(HTTPStatus.OK, service.capture_retail_mapping(
                    str(payload["comparable_key"]), str(payload["retailer"]),
                    confirmed_region=str(payload.get("confirmed_region") or "") or None))
            else:
                self.send_error(HTTPStatus.NOT_FOUND)

        def _static(self, path: str) -> None:
            relative = "index.html" if path in {"", "/"} else path.lstrip("/")
            candidate = (STATIC_DIR / relative).resolve()
            if STATIC_DIR.resolve() not in candidate.parents:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            try:
                content = candidate.read_bytes()
            except FileNotFoundError:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            self.send_response(HTTPStatus.OK)
            self.send_header(
                "Content-Type",
                mimetypes.guess_type(candidate.name)[0] or "application/octet-stream",
            )
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(content)

        def _body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0"))
            if not length:
                return {}
            value = json.loads(self.rfile.read(length))
            if not isinstance(value, dict):
                raise ValueError("JSON request body must be an object")
            return value

        def _json(self, status: HTTPStatus, payload: object) -> None:
            content = json.dumps(payload, default=_json_default, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(content)

        def _error(self, error: Exception) -> None:
            status = (
                HTTPStatus.NOT_FOUND
                if isinstance(error, KeyError)
                else HTTPStatus.BAD_REQUEST
                if isinstance(error, (ValueError, TypeError))
                else HTTPStatus.INTERNAL_SERVER_ERROR
            )
            self._json(status, {"error": str(error)})

        def _path_parts(self) -> list[str]:
            return [unquote(part) for part in urlsplit(self.path).path.split("/") if part]

        def _path_after(self, prefix: str) -> str:
            path = urlsplit(self.path).path
            if not path.startswith(prefix):
                raise KeyError(path)
            return unquote(path[len(prefix) :].strip("/"))

    return Handler


def run(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    service = GuiService(
        config_path=args.config,
        database_path=args.database,
        output_dir=args.output_dir,
        debug_dir=args.debug_dir,
    )
    server = ThreadingHTTPServer((args.host, args.port), create_handler(service))
    url = f"http://{args.host}:{server.server_port}"
    print(f"Resell Monitor GUI: {url}")
    if not args.no_browser:
        threading.Timer(0.25, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 130
    finally:
        server.shutdown()
        server.server_close()
        service.close()
    return 0


def _json_default(value: object) -> object:
    if isinstance(value, datetime):
        return value.isoformat()
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"cannot encode {type(value).__name__}")


if __name__ == "__main__":
    raise SystemExit(run())
