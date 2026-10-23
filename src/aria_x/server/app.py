"""HTTP transport for the ARIA-X control plane.

Responsibilities are intentionally narrow:

* bind a :class:`ThreadingHTTPServer` (one thread per request, so a live SSE
  stream never starves the API);
* authenticate API calls when ``config.server.auth_token`` is set;
* serve the bundled, offline web console from ``aria_x/web`` with traversal
  protection and no directory listings;
* translate exceptions into the documented JSON error envelope.

The API itself lives in :mod:`aria_x.server.api`.
"""

from __future__ import annotations

import hmac
import json
import logging
import mimetypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import parse_qs, unquote, urlsplit

from .. import TAGLINE, __version__
from ..config import AriaXConfig
from ..errors import AriaXError
from ..events import EventBus
from ..store import Store
from ..util import json_dumps
from .api import Api, RequestError, Response, StreamResponse, response_for_error

logger = logging.getLogger("aria_x.server")

WEB_ROOT = (Path(__file__).resolve().parent.parent / "web").resolve()
MAX_BODY_BYTES = 8 * 1024 * 1024

_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".webmanifest": "application/manifest+json; charset=utf-8",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".txt": "text/plain; charset=utf-8",
}

_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; connect-src 'self'; font-src 'self'; frame-src 'self'; "
    "base-uri 'none'; form-action 'self'; object-src 'none'"
)


# ---------------------------------------------------------------------------
# server
# ---------------------------------------------------------------------------


class AriaXHTTPServer(ThreadingHTTPServer):
    """Threading server carrying the API collaborators for each request."""

    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 64

    def __init__(self, server_address: tuple[str, int], handler_cls: type[BaseHTTPRequestHandler], *,
                 config: AriaXConfig, api: Api) -> None:
        self.config = config
        self.api = api
        self.store = api.store
        self.bus = api.bus
        self.engagement = api.engagement
        super().__init__(server_address, handler_cls)

    @property
    def url(self) -> str:
        host, port = self.server_address[:2]
        return f"http://{host}:{port}"

    def server_bind(self) -> None:  # keep tests on ephemeral ports reliable
        super().server_bind()


# ---------------------------------------------------------------------------
# request handler
# ---------------------------------------------------------------------------


class AriaXRequestHandler(BaseHTTPRequestHandler):
    """Dispatch GET/POST/HEAD/OPTIONS for the API and the static console."""

    protocol_version = "HTTP/1.1"
    server_version = f"ARIA-X/{__version__}"
    sys_version = ""

    def version_string(self) -> str:
        return f"ARIA-X/{__version__}"

    # -- verb entry points -------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        self._handle("GET")

    def do_HEAD(self) -> None:  # noqa: N802
        self._handle("HEAD")

    def do_POST(self) -> None:  # noqa: N802
        self._handle("POST")

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._handle("OPTIONS")

    # -- logging -----------------------------------------------------------

    def log_message(self, fmt: str, *args: Any) -> None:
        logger.debug("%s - %s", self.address_string(), fmt % args)

    def log_error(self, fmt: str, *args: Any) -> None:
        logger.debug("%s - %s", self.address_string(), fmt % args)

    # -- core dispatch -----------------------------------------------------

    def _handle(self, method: str) -> None:
        try:
            split = urlsplit(self.path)
            path = unquote(split.path or "/")
            query = parse_qs(split.query, keep_blank_values=True)
            if method == "OPTIONS":
                self._handle_options(path)
                return
            if path in {"/", "/index.html"}:
                if method not in {"GET", "HEAD"}:
                    self._method_not_allowed("GET, HEAD")
                    return
                self._serve_static("index.html", head=(method == "HEAD"))
                return
            if path.startswith("/static/"):
                if method not in {"GET", "HEAD"}:
                    self._method_not_allowed("GET, HEAD")
                    return
                self._serve_static(path[len("/static/"):], head=(method == "HEAD"))
                return
            if path == "/favicon.ico":
                self._respond(Response.bytes(b"", status=204, content_type="image/x-icon"),
                              head=(method == "HEAD"))
                return
            if path == "/api" or path.startswith("/api/"):
                self._handle_api(method, path, query)
                return
            self._respond(Response.error("not_found", f"no such route: {path}", 404),
                          head=(method == "HEAD"))
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception:  # pragma: no cover - defensive: never leak tracebacks
            logger.exception("unhandled server error for %s %s", method, self.path)
            try:
                self._respond(Response.error("internal_error", "internal server error", 500))
            except Exception:
                pass

    def _handle_api(self, method: str, path: str, query: Mapping[str, Sequence[str]]) -> None:
        if not self._authorized(path, query):
            self._respond(Response.error("unauthorized", "missing or invalid bearer token", 401),
                          extra_headers={"WWW-Authenticate": 'Bearer realm="ARIA-X"'})
            return
        if method not in {"GET", "POST"}:
            self._method_not_allowed("GET, POST")
            return
        body: dict[str, Any] | None = None
        if method == "POST":
            try:
                body = self._read_json_body()
            except RequestError as exc:
                self._respond(response_for_error(exc))
                return
        try:
            result = self.server.api.dispatch(method, path, query, body)  # type: ignore[attr-defined]
        except RequestError as exc:
            self._respond(response_for_error(exc), extra_headers=exc.headers)
            return
        except AriaXError as exc:
            logger.debug("API error %s: %s %s", exc.code, method, path)
            self._respond(response_for_error(exc))
            return
        except BaseException as exc:  # noqa: BLE001 - translated by response_for_error
            logger.exception("API request failed: %s %s", method, path)
            self._respond(response_for_error(exc))
            return
        if isinstance(result, StreamResponse):
            self._stream(result)
        elif isinstance(result, Response):
            self._respond(result)
        else:  # pragma: no cover - defensive programming
            self._respond(Response.error("internal_error", "invalid handler response", 500))

    def _handle_options(self, path: str) -> None:
        self.send_response(204)
        for key, value in self._common_headers().items():
            self.send_header(key, value)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, HEAD, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type, X-ARIA-X-Token")
        self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _method_not_allowed(self, allow: str) -> None:
        self._respond(Response.error("method_not_allowed", "method not allowed", 405),
                      extra_headers={"Allow": allow})

    # -- auth --------------------------------------------------------------

    def _authorized(self, path: str, query: Mapping[str, Sequence[str]]) -> bool:
        token = str(getattr(self.server.config.server, "auth_token", "") or "")  # type: ignore[attr-defined]
        if not token:
            return True
        normalised = path.rstrip("/") or "/"
        if normalised == "/api/health":
            return True
        header = self.headers.get("Authorization", "") or ""
        if header.lower().startswith("bearer "):
            if hmac.compare_digest(header[7:].strip(), token):
                return True
        supplied = self.headers.get("X-ARIA-X-Token", "") or ""
        if supplied and hmac.compare_digest(supplied, token):
            return True
        # EventSource cannot set headers, so the SSE route also accepts ?token=.
        if normalised.endswith("/events"):
            candidate = (query.get("token") or [""])[0]
            if candidate and hmac.compare_digest(str(candidate), token):
                return True
        return False

    # -- body --------------------------------------------------------------

    def _read_json_body(self) -> dict[str, Any]:
        raw_length = self.headers.get("Content-Length", "0") or "0"
        try:
            length = int(raw_length)
        except (TypeError, ValueError):
            raise RequestError("invalid Content-Length header") from None
        if length < 0:
            raise RequestError("invalid Content-Length header")
        if length > MAX_BODY_BYTES:
            raise RequestError("request body too large", status=413, code="payload_too_large")
        if length == 0:
            return {}
        data = self.rfile.read(length)
        if not data:
            return {}
        try:
            parsed = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise RequestError("request body must be valid JSON", code="invalid_json") from None
        if not isinstance(parsed, dict):
            raise RequestError("JSON body must be an object", code="invalid_json")
        return parsed

    # -- responses ---------------------------------------------------------

    def _common_headers(self) -> dict[str, str]:
        headers = {
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "SAMEORIGIN",
            "Referrer-Policy": "no-referrer",
        }
        origin = str(getattr(self.server.config.server, "cors_origin", "") or "")  # type: ignore[attr-defined]
        if origin:
            headers["Access-Control-Allow-Origin"] = origin
            headers["Vary"] = "Origin"
        return headers

    def _respond(self, response: Response, *, head: bool = False,
                 extra_headers: Mapping[str, str] | None = None) -> None:
        self.send_response(response.status)
        for key, value in self._common_headers().items():
            self.send_header(key, value)
        for key, value in response.headers.items():
            self.send_header(key, value)
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Type", response.content_type)
        self.send_header("Content-Length", str(len(response.body)))
        if "Cache-Control" not in response.headers:
            self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if not head and response.body:
            self.wfile.write(response.body)
            self.wfile.flush()

    def _stream(self, stream: StreamResponse) -> None:
        self.send_response(stream.status)
        for key, value in self._common_headers().items():
            self.send_header(key, value)
        for key, value in stream.headers.items():
            self.send_header(key, value)
        self.send_header("Content-Type", stream.content_type)
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        self.close_connection = True
        chunks = stream.chunks
        try:
            self.wfile.flush()
            for chunk in chunks:
                if isinstance(chunk, str):
                    chunk = chunk.encode("utf-8")
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            close = getattr(chunks, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass

    # -- static console ----------------------------------------------------

    def _serve_static(self, relative: str, *, head: bool = False) -> None:
        if not relative or relative.endswith("/"):
            self._respond(Response.error("not_found", "not found", 404), head=head)
            return
        if "\x00" in relative or relative.startswith("/"):
            self._respond(Response.error("not_found", "not found", 404), head=head)
            return
        candidate = (WEB_ROOT / relative).resolve()
        try:
            candidate.relative_to(WEB_ROOT)
        except ValueError:
            self._respond(Response.error("not_found", "not found", 404), head=head)
            return
        if not candidate.is_file():
            self._respond(Response.error("not_found", f"no such asset: {relative}", 404), head=head)
            return
        try:
            payload = candidate.read_bytes()
        except OSError:
            self._respond(Response.error("not_found", "asset unavailable", 404), head=head)
            return
        content_type = _CONTENT_TYPES.get(candidate.suffix.lower())
        if content_type is None:
            guessed = mimetypes.guess_type(str(candidate))[0]
            content_type = (guessed + "; charset=utf-8") if guessed and guessed.startswith("text/") \
                else (guessed or "application/octet-stream")
        headers: dict[str, str] = {"Cache-Control": "no-cache"}
        if content_type.startswith("text/html"):
            headers["Content-Security-Policy"] = _CSP
        self._respond(Response.bytes(payload, content_type=content_type, headers=headers), head=head)


# ---------------------------------------------------------------------------
# factories
# ---------------------------------------------------------------------------


def create_server(config: AriaXConfig, engagement: Any = None,
                  bus: EventBus | None = None, store: Store | None = None) -> AriaXHTTPServer:
    """Build (but do not start) the HTTP server bound to ``config.server``.

    ``port = 0`` is honoured so tests can bind an ephemeral port; callers read
    the actual address from ``server.server_address``.
    """
    if store is None:
        store = getattr(engagement, "store", None)
    if bus is None:
        bus = getattr(engagement, "events", None) or getattr(engagement, "bus", None)
    if store is None:
        raise ValueError("create_server requires a Store (pass store= or attach one to engagement)")
    if bus is None:
        raise ValueError("create_server requires an EventBus (pass bus= or attach one to engagement)")
    api = Api(config, engagement, store=store, bus=bus)
    host = str(config.server.host or "127.0.0.1")
    port = int(config.server.port or 0)
    server = AriaXHTTPServer((host, port), AriaXRequestHandler, config=config, api=api)
    logger.info("ARIA-X %s console listening on %s (%s)", __version__, server.url, TAGLINE)
    return server


def serve(config: AriaXConfig, *, engagement: Any = None, store: Store | None = None,
          bus: EventBus | None = None, block: bool = True) -> AriaXHTTPServer | None:
    """Run the console, creating any missing collaborators.

    With ``block=False`` the server is returned unstarted (the caller owns its
    lifecycle). With ``block=True`` this call blocks until Ctrl-C or another
    thread calls ``server.shutdown()``; resources created here are closed.
    """
    owned: list[Any] = []
    if store is None:
        Path(config.data_dir).mkdir(parents=True, exist_ok=True)
        store = Store(config.db_path)
        owned.append(store)
    if bus is None:
        bus = EventBus(log_path=Path(config.data_dir) / "events.jsonl",
                       redact=bool(config.safety.redact_secrets))
        owned.append(bus)
    if engagement is None:
        try:
            from ..engine import Engagement
        except Exception as exc:  # pragma: no cover - during parallel module development
            raise RuntimeError(
                "engagement engine is unavailable; start the console with an explicit engagement"
            ) from exc
        engagement = Engagement(config, store=store, events=bus)
        owned.append(engagement)
    server = create_server(config, engagement, store=store, bus=bus)
    if not block:
        return server
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        logger.info("console interrupted")
    finally:
        try:
            server.server_close()
        finally:
            for resource in reversed(owned):
                try:
                    resource.close()
                except Exception:
                    pass
    return None
