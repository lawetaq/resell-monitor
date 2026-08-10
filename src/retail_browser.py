from __future__ import annotations

import json
import queue
import shutil
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Literal, Protocol
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from src.retail_browser_adapters import ADAPTERS, RetailBrowserAdapter
from src.retail_browser_models import BrowserNetworkPayload, BrowserPageSnapshot


class RetailBrowserError(RuntimeError):
    pass


RetailBrowserEngine = Literal["firefox", "chromium"]
RETAIL_BROWSER_ENGINES = ("firefox", "chromium")


class RetailBrowserRuntime(Protocol):
    def launch(self) -> None: ...
    def navigate(self, retailer: str, url: str) -> dict[str, object]: ...
    def capture(self, retailer: str) -> BrowserPageSnapshot: ...
    def pages(self) -> list[dict[str, object]]: ...
    def close(self) -> None: ...


@dataclass(slots=True, frozen=True)
class RetailBrowserStatus:
    state: str
    error: str | None
    engine: RetailBrowserEngine
    profile_path: str
    pages: tuple[dict[str, object], ...] = ()


RuntimeFactory = Callable[[Path, Callable[[], None]], RetailBrowserRuntime]


class RetailBrowserService:
    """Thread-owned, explicit-command browser service.

    Playwright's synchronous objects never leave the worker that created them.
    Construction and status reads perform no browser or marketplace operation.
    """

    def __init__(self, *, engine: RetailBrowserEngine = "firefox",
                 profile_root: Path = Path("data/playwright"),
                 runtime_factory: RuntimeFactory | None = None,
                 command_timeout: float = 65.0) -> None:
        self._engine = _validate_engine(engine)
        self.profile_root = profile_root
        self.runtime_factory = runtime_factory or _runtime_factory
        self.command_timeout = command_timeout
        self._lock = threading.RLock()
        self._state = "closed"
        self._error: str | None = None
        self._thread: threading.Thread | None = None
        self._commands: queue.Queue[tuple[str, tuple[object, ...], threading.Event,
                                                 dict[str, object]]] = queue.Queue()
        self._ready: threading.Event | None = None

    @property
    def engine(self) -> RetailBrowserEngine:
        return self._engine

    @property
    def profile_dir(self) -> Path:
        return self.profile_root / f"retail-{self.engine}-profile"

    def set_engine(self, engine: str) -> bool:
        selected = _validate_engine(engine)
        with self._lock:
            if self._state in {"starting", "open"}:
                raise RetailBrowserError("Close the Retail Browser before changing its engine.")
            if selected == self._engine:
                return False
            self._engine = selected
            self._error = None
        return True

    def status(self) -> RetailBrowserStatus:
        with self._lock:
            state, error = self._state, self._error
        pages: tuple[dict[str, object], ...] = ()
        if state == "open":
            try:
                pages = tuple(self._call("pages"))  # type: ignore[arg-type]
            except RetailBrowserError:
                with self._lock:
                    state, error = self._state, self._error
        return RetailBrowserStatus(state, error, self.engine, str(self.profile_dir), pages)

    def open(self) -> bool:
        with self._lock:
            if self._state in {"starting", "open"}:
                return False
            stale_worker = self._thread is not None and self._thread.is_alive()
        if stale_worker:
            self.close()
        with self._lock:
            self._state = "starting"
            self._error = None
            self._ready = threading.Event()
            self._thread = threading.Thread(target=self._worker,
                                            name="resell-monitor-retail-browser",
                                            daemon=True)
            self._thread.start()
            ready = self._ready
        if not ready.wait(self.command_timeout):
            self._set_error("Retail browser launch timed out")
            raise RetailBrowserError("Retail browser launch timed out")
        with self._lock:
            if self._state != "open":
                raise RetailBrowserError(self._error or "Retail browser failed to open")
        return True

    def navigate(self, retailer: str, url: str) -> dict[str, object]:
        adapter = _adapter(retailer)
        valid, _ = adapter.validate_url(url)
        return self._call("navigate", retailer, valid)  # type: ignore[return-value]

    def capture(self, retailer: str) -> BrowserPageSnapshot:
        _adapter(retailer)
        return self._call("capture", retailer)  # type: ignore[return-value]

    def close(self) -> bool:
        with self._lock:
            thread = self._thread
            if thread is None or not thread.is_alive():
                self._state = "closed"
                self._thread = None
                return False
        try:
            self._call("close", require_open=False)
        finally:
            thread.join(timeout=10)
            with self._lock:
                self._thread = None
                self._state = "closed"
        return True

    def reset_profile(self) -> bool:
        self.close()
        target = self.profile_dir.resolve(strict=False)
        expected_name = f"retail-{self.engine}-profile"
        if target.name != expected_name or target.parent.name != "playwright":
            raise ValueError(f"refusing to reset anything except a dedicated playwright/{expected_name}")
        if self.profile_dir.is_symlink():
            raise ValueError("retail profile path must not be a symbolic link")
        if not target.exists():
            return False
        if not target.is_dir():
            raise ValueError("retail profile path is not a directory")
        shutil.rmtree(target)
        return True

    def _worker(self) -> None:
        runtime = self.runtime_factory(self.profile_dir, self._browser_closed)
        try:
            runtime.launch()
            with self._lock:
                self._state = "open"
            assert self._ready is not None
            self._ready.set()
            while True:
                action, arguments, completed, output = self._commands.get()
                try:
                    if action == "close":
                        output["value"] = True
                        completed.set()
                        break
                    output["value"] = getattr(runtime, action)(*arguments)
                except Exception as error:
                    output["error"] = error
                finally:
                    completed.set()
        except Exception as error:
            self._set_error(_safe_error(error))
            if self._ready is not None:
                self._ready.set()
        finally:
            try:
                runtime.close()
            except Exception:
                pass
            with self._lock:
                if self._state != "error":
                    self._state = "closed"

    def _call(self, action: str, *arguments: object,
              require_open: bool = True) -> object:
        with self._lock:
            if require_open and self._state != "open":
                raise RetailBrowserError(
                    self._error or "Retail browser is not open. Open it explicitly first."
                )
            thread = self._thread
            if thread is None or not thread.is_alive():
                self._state = "closed"
                raise RetailBrowserError("Retail browser process is not running")
        completed = threading.Event()
        output: dict[str, object] = {}
        self._commands.put((action, arguments, completed, output))
        if not completed.wait(self.command_timeout):
            self._set_error(f"Retail browser {action} timed out")
            raise RetailBrowserError(f"Retail browser {action} timed out")
        if "error" in output:
            error = output["error"]
            message = _safe_error(error if isinstance(error, Exception)
                                  else RuntimeError(str(error)))
            self._set_error(message)
            raise RetailBrowserError(message)
        return output.get("value")

    def _browser_closed(self) -> None:
        with self._lock:
            if self._state == "open":
                self._state = "closed"

    def _set_error(self, message: str) -> None:
        with self._lock:
            self._state = "error"
            self._error = message


class PlaywrightRetailRuntime:
    def __init__(self, profile_dir: Path, on_closed: Callable[[], None],
                 *, engine: RetailBrowserEngine = "firefox",
                 navigation_timeout_ms: int = 60_000) -> None:
        self.profile_dir = profile_dir
        self.on_closed = on_closed
        self.engine = _validate_engine(engine)
        self.navigation_timeout_ms = navigation_timeout_ms
        self._playwright: Any | None = None
        self._context: Any | None = None
        self._pages: dict[str, Any] = {}
        self._network: dict[str, list[BrowserNetworkPayload]] = {}

    def launch(self) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as error:
            raise RetailBrowserError(
                "Playwright is not installed. Run: pip install -r requirements.txt"
            ) from error
        self.profile_dir.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._playwright = sync_playwright().start()
            self._context = getattr(self._playwright, self.engine).launch_persistent_context(
                user_data_dir=self.profile_dir, headless=False, locale="ru-RU",
            )
            self._context.on("close", lambda: self.on_closed())
        except Exception as error:
            raise RetailBrowserError(
                f"Playwright {self.engine.capitalize()} is not installed or could not start.\n"
                f"Run:\npython -m playwright install {self.engine}\n"
                "The dedicated retail profile may also be locked. "
                f"Details: {_safe_error(error)}"
            ) from error

    def navigate(self, retailer: str, url: str) -> dict[str, object]:
        adapter = _adapter(retailer)
        page = self._pages.get(retailer)
        if page is None or page.is_closed():
            page = self._context.new_page()
            self._pages[retailer] = page
            self._network[retailer] = []
            page.on("response", lambda response, name=retailer: self._capture_response(name, response))
        self._network[retailer].clear()
        try:
            response = page.goto(url, wait_until="domcontentloaded",
                                 timeout=self.navigation_timeout_ms)
        except Exception as error:
            raise RetailBrowserError(f"Navigation failed or timed out: {_safe_error(error)}") from error
        return {"retailer": retailer, "url": page.url,
                "status": response.status if response is not None else None,
                "title": page.title()}

    def capture(self, retailer: str) -> BrowserPageSnapshot:
        page = self._pages.get(retailer)
        if page is None or page.is_closed():
            raise RetailBrowserError(f"No open {retailer} page. Open its mapping first.")
        return BrowserPageSnapshot(retailer, page.url, page.title(), page.content(),
                                   tuple(self._network.get(retailer, ())))

    def pages(self) -> list[dict[str, object]]:
        return [{"retailer": retailer, "url": page.url, "title": page.title()}
                for retailer, page in self._pages.items() if not page.is_closed()]

    def close(self) -> None:
        if self._context is not None:
            self._context.close()
            self._context = None
        if self._playwright is not None:
            self._playwright.stop()
            self._playwright = None

    def _capture_response(self, retailer: str, response: object) -> None:
        adapter = _adapter(retailer)
        try:
            url = response.url  # type: ignore[attr-defined]
            content_type = response.header_value("content-type") or ""  # type: ignore[attr-defined]
            if not adapter.relevant_response(url, content_type):
                return
            payload = _sanitize_payload(response.json())  # type: ignore[attr-defined]
            if len(json.dumps(payload, ensure_ascii=False)) > 2_000_000:
                return
            rows = self._network.setdefault(retailer, [])
            rows.append(BrowserNetworkPayload(_safe_network_url(url),
                                              response.status, content_type, payload))  # type: ignore[attr-defined]
            del rows[:-50]
        except Exception:
            return


def _runtime_factory(profile_dir: Path,
                     on_closed: Callable[[], None]) -> RetailBrowserRuntime:
    engine = "firefox" if profile_dir.name == "retail-firefox-profile" else "chromium"
    return PlaywrightRetailRuntime(profile_dir, on_closed, engine=engine)


def _validate_engine(engine: str) -> RetailBrowserEngine:
    if engine not in RETAIL_BROWSER_ENGINES:
        raise ValueError(f"unsupported retail browser engine: {engine}")
    return engine  # type: ignore[return-value]


def _adapter(retailer: str) -> RetailBrowserAdapter:
    try:
        return ADAPTERS[retailer]
    except KeyError as error:
        raise ValueError(f"unsupported retail browser provider: {retailer}") from error


def _safe_network_url(url: str) -> str:
    parts = urlsplit(url)
    query = parse_qs(parts.query)
    allowed = {name: values[0] for name, values in query.items()
               if name in {"dest", "nm", "nmId", "product_id"} and values}
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(allowed), ""))


def _sanitize_payload(value: object) -> object:
    sensitive = {"cookie", "authorization", "token", "access_token",
                 "refresh_token", "session", "user_id", "account_id"}
    if isinstance(value, dict):
        return {str(key): _sanitize_payload(child) for key, child in value.items()
                if str(key).casefold() not in sensitive}
    if isinstance(value, list):
        return [_sanitize_payload(child) for child in value]
    return value


def _safe_error(error: Exception) -> str:
    text = str(error)
    return text[:500] if text else type(error).__name__


def status_payload(status: RetailBrowserStatus) -> dict[str, object]:
    return asdict(status)
