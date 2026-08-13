from __future__ import annotations

import argparse
from dataclasses import dataclass
import importlib
import ipaddress
import logging
from pathlib import Path
import secrets
import sys
from types import ModuleType
from urllib.parse import urlsplit
import webbrowser

from src.gui.app import GuiServer, create_server
from src.resources import is_frozen, resource_path
from src.user_paths import UserPaths, prepare_installed_user_data


LOOPBACK_HOST = "127.0.0.1"
LOGGER = logging.getLogger(__name__)


class DesktopDependencyError(RuntimeError):
    """Desktop shell dependency is unavailable."""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Resell Monitor desktop application")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--database", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--debug-dir", type=Path)
    parser.add_argument(
        "--legacy-root", type=Path,
        help="trusted legacy project root to copy from when installed data is empty",
    )
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--gui", choices=("qt", "gtk"))
    return parser


@dataclass(slots=True, frozen=True)
class DesktopPaths:
    config: Path
    database: Path
    output_dir: Path
    debug_dir: Path | None


def resolve_desktop_paths(
    args: argparse.Namespace, *, packaged: bool | None = None,
    user_paths: UserPaths | None = None,
) -> DesktopPaths:
    """Resolve mutable paths without changing source-mode developer defaults."""

    installed = is_frozen() if packaged is None else packaged
    if not installed:
        return DesktopPaths(
            args.config or Path("searches.json"),
            args.database or Path("data/resell-monitor.db"),
            args.output_dir or Path("output"),
            args.debug_dir,
        )
    if args.config is not None and args.database is not None and args.output_dir is not None:
        return DesktopPaths(args.config, args.database, args.output_dir, args.debug_dir)
    paths = user_paths or UserPaths.from_platform()
    legacy_root = getattr(args, "legacy_root", None)
    if legacy_root is None:
        legacy_root = _default_legacy_root()
    prepare_installed_user_data(paths, legacy_root=legacy_root)
    return DesktopPaths(
        args.config or paths.searches_path,
        args.database or paths.database_path,
        args.output_dir or paths.export_dir,
        args.debug_dir,
    )


def _default_legacy_root() -> Path:
    """Return one tightly scoped legacy root for an installed executable."""

    executable_parent = Path(sys.executable).resolve().parent
    # A repository build lives at dist/ResellMonitor/ResellMonitor. This exact
    # layout is the only project-local location discovered automatically.
    if executable_parent.parent.name == "dist":
        return executable_parent.parent.parent
    return executable_parent


def configure_packaged_logging(paths: UserPaths | None = None) -> Path:
    """Configure a persistent packaged-mode log without touching source mode."""

    installed_paths = paths or UserPaths.from_platform()
    installed_paths.create_directories()
    log_path = installed_paths.log_dir / "resell-monitor.log"
    logging.basicConfig(
        filename=log_path,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    return log_path


def new_session_token() -> str:
    """Create a non-persistent, cryptographically random desktop session token."""

    return secrets.token_urlsafe(32)


def load_webview() -> ModuleType:
    try:
        return importlib.import_module("webview")
    except ImportError as error:
        raise DesktopDependencyError(
            "Desktop mode requires pywebview. Install requirements-desktop.txt first."
        ) from error


def validate_external_url(url: str, *, internal_origin: str | None = None) -> str:
    """Validate a public browser destination for the narrow native bridge."""

    candidate = url.strip()
    try:
        parsed = urlsplit(candidate)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as error:
        raise ValueError("invalid external URL") from error
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("external URL must be HTTP(S) without credentials")
    host = hostname.casefold().rstrip(".")
    if host == "localhost" or host.endswith(".localhost"):
        raise ValueError("local application URLs cannot be opened externally")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise ValueError("local or private URLs cannot be opened externally")
    if internal_origin is not None:
        origin = urlsplit(internal_origin)
        effective_port = port or (443 if parsed.scheme == "https" else 80)
        origin_port = origin.port or (443 if origin.scheme == "https" else 80)
        if (
            parsed.scheme.casefold() == origin.scheme.casefold()
            and host == (origin.hostname or "").casefold().rstrip(".")
            and effective_port == origin_port
        ):
            raise ValueError("application URLs stay inside the desktop window")
    return candidate


class DesktopBridge:
    """The only native capability exposed to the application JavaScript."""

    def __init__(self, internal_origin: str) -> None:
        self.internal_origin = internal_origin

    def open_external_url(self, url: str) -> bool:
        return bool(
            webbrowser.open(
                validate_external_url(url, internal_origin=self.internal_origin),
                new=2,
            )
        )


def launch_desktop(args: argparse.Namespace, webview: ModuleType) -> int:
    token = new_session_token()
    server: GuiServer | None = None
    try:
        paths = resolve_desktop_paths(args)
        server = create_server(
            config_path=paths.config,
            database_path=paths.database,
            output_dir=paths.output_dir,
            debug_dir=paths.debug_dir,
            host=LOOPBACK_HOST,
            port=0,
            desktop_session_token=token,
        )
        server.start()
        bootstrap_url = f"{server.url}/desktop/{token}/"
        bridge = DesktopBridge(server.url)
        webview.create_window(
            "Resell Monitor",
            bootstrap_url,
            width=1400,
            height=880,
            min_size=(1100, 700),
            resizable=True,
            js_api=bridge,
        )
        options: dict[str, object] = {"debug": bool(args.debug)}
        options["icon"] = str(resource_path("assets/branding/resell-monitor-256.png"))
        if args.gui:
            options["gui"] = args.gui
        webview.start(**options)
        return 0
    finally:
        if server is not None:
            server.stop()


def run(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if is_frozen():
            configure_packaged_logging()
        webview = load_webview()
        return launch_desktop(args, webview)
    except KeyboardInterrupt:
        return 130
    except DesktopDependencyError as error:
        LOGGER.error("Desktop dependency unavailable: %s", error)
        print(str(error), file=sys.stderr)
        return 2
    except Exception as error:
        LOGGER.exception("Desktop startup failed")
        print(f"Resell Monitor desktop could not start: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(run())
