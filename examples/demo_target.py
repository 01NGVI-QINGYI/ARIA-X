#!/usr/bin/env python3
"""ARIA-X demo target — a deliberately vulnerable, localhost-only HTTP app.

WARNING
=======
This program is intentionally insecure and exists only to exercise ARIA-X against a
target that is safe to break. It binds **127.0.0.1 only**, refuses to serve any
file outside its temporary root, and contains no destructive behavior.

Intentional weaknesses (for scanner demos):
  1. Path traversal in ``/files?path=...`` — a naive ``os.path.join`` lets
     ``../flag.txt`` escape the public directory. The handler *still* only ever
     reads one of two allow-listed files (see ``_resolve``), so no arbitrary
     host file can be disclosed.
  2. Missing security headers — no CSP, HSTS, X-Content-Type-Options,
     X-Frame-Options or Referrer-Policy on any response.
  3. ``/debug`` leaks a fake build version and ``debug=true``.

Protocol for tooling (``--json``): print exactly one JSON object line to stdout
after binding, then keep serving::

    {"port": 53123, "url": "http://127.0.0.1:53123", "flag": "ARIA-X{demo_path_traversal_confirmed}"}

Run::

    python3 examples/demo_target.py --port 0 --json
    python3 examples/demo_target.py --port 8000

Stop with Ctrl-C or SIGTERM.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import socketserver
import sys
import tempfile
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

FLAG = "ARIA-X{demo_path_traversal_confirmed}"
FAKE_VERSION = "demo-target/0.3.7-internal-debug"
HOST = "127.0.0.1"

BANNER = r"""
  ___   ___  ___ ___
 / _ \ | _ \| __/ __|   demo target — INTENTIONALLY VULNERABLE
| (_) ||   /| _|\__ \   localhost only · use for authorized testing
 \___/ |_|_\|___|___/   flag: ARIA-X{demo_path_traversal_confirmed}
"""


class DemoState:
    """Filesystem state for one demo-target instance (a throw-away temp root)."""

    def __init__(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="aria-x-demo-target-"))
        self.public = self.root / "public"
        self.public.mkdir(parents=True, exist_ok=True)
        self.flag_path = self.root / "flag.txt"
        self.welcome_path = self.public / "welcome.txt"
        self.flag_path.write_text(FLAG + "\n", encoding="utf-8")
        self.welcome_path.write_text(
            "Welcome to the ARIA-X demo target.\n\n"
            "This service is intentionally vulnerable:\n"
            "  - /files?path=welcome.txt   (path traversal primitive)\n"
            "  - /debug                   (verbose build/debug info)\n\n"
            "It only reads files inside its temporary root.\n",
            encoding="utf-8",
        )

    def resolve(self, attacker_path: str) -> Path | None:
        """Naive join (the bug), followed by a hard allow-list (the safety rail).

        ``os.path.join`` + ``Path`` resolution is deliberately *not* confined to
        ``public/``: ``../flag.txt`` escapes it, which is the vulnerability we
        want scanners to find. The allow-list then keeps the demo harmless.
        """
        candidate = Path(os.path.join(str(self.public), attacker_path))
        try:
            resolved = candidate.resolve()
        except OSError:
            return None
        if resolved == self.flag_path.resolve():
            return resolved
        try:
            resolved.relative_to(self.public.resolve())
        except ValueError:
            return None
        return resolved


INDEX_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>ARIA-X Demo Target</title></head>
<body>
  <h1>ARIA-X demo target</h1>
  <p>Intentionally vulnerable service for authorized, local testing only.</p>
  <ul>
    <li><a href="/files?path=welcome.txt">/files?path=welcome.txt</a> — file viewer</li>
    <li><a href="/debug">/debug</a> — build/debug information</li>
  </ul>
</body></html>
"""


class DemoHandler(BaseHTTPRequestHandler):
    server_version = "AresDemoTarget/0.3.7"
    sys_version = ""

    # -- plumbing --------------------------------------------------------

    def log_message(self, fmt: str, *args: object) -> None:  # noqa: D401 - stdlib hook
        if getattr(self.server, "quiet", False):
            return
        sys.stderr.write("[demo-target] %s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, status: int, body: bytes = b"", content_type: str = "text/plain; charset=utf-8",
              extra_headers: dict[str, str] | None = None) -> None:
        # Intentional weakness: no CSP / HSTS / X-Frame-Options / nosniff.
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, payload: dict[str, object]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    # -- routes ----------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        parts = urlsplit(self.path)
        route = parts.path
        if route in {"", "/"}:
            self._send(HTTPStatus.OK, INDEX_HTML.encode("utf-8"), "text/html; charset=utf-8")
            return
        if route == "/files":
            self._files(parts.query)
            return
        if route == "/debug":
            self._json(HTTPStatus.OK, {
                "debug": True,
                "version": FAKE_VERSION,
                "python": sys.version.split()[0],
                "root": str(self.server.state.root),
                "flags": {"demo": True, "auth": False},
            })
            return
        if route == "/robots.txt":
            self._send(HTTPStatus.OK, b"User-agent: *\nDisallow: /files\nDisallow: /debug\n")
            return
        if route == "/healthz":
            self._json(HTTPStatus.OK, {"ok": True})
            return
        self._send(HTTPStatus.NOT_FOUND, b"not found\n")

    def do_HEAD(self) -> None:  # noqa: N802 - stdlib naming
        self.do_GET()

    def _files(self, query: str) -> None:
        params = parse_qs(query, keep_blank_values=True)
        requested = (params.get("path") or [""])[0]
        if not requested:
            self._send(HTTPStatus.BAD_REQUEST, b"missing path parameter\n")
            return
        target = self.server.state.resolve(requested)
        if target is None:
            # Looks like a normal 404: we do not disclose whether the file exists.
            self._send(HTTPStatus.NOT_FOUND, b"file not found\n")
            return
        try:
            data = target.read_bytes()
        except OSError:
            self._send(HTTPStatus.NOT_FOUND, b"file not found\n")
            return
        self._send(HTTPStatus.OK, data, "text/plain; charset=utf-8",
                   extra_headers={"X-Demo-File": target.name})


class DemoHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer without the reverse-DNS lookup in ``server_bind``.

    ``HTTPServer.server_bind`` calls ``socket.getfqdn(host)``; on machines with a
    broken or slow resolver that can block for tens of seconds even for
    127.0.0.1. The demo must report its port immediately, so bind the socket and
    fill in the two informational attributes ourselves.
    """

    daemon_threads = True

    def server_bind(self) -> None:  # noqa: D401 - stdlib override
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = host
        self.server_port = port


def build_server(port: int = 0, *, quiet: bool = False) -> DemoHTTPServer:
    state = DemoState()
    server = DemoHTTPServer((HOST, int(port)), DemoHandler)
    server.daemon_threads = True
    server.state = state  # type: ignore[attr-defined]
    server.quiet = quiet   # type: ignore[attr-defined]
    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="ARIA-X deliberately vulnerable localhost demo target (authorized testing only).",
    )
    parser.add_argument("--port", type=int, default=0,
                        help="TCP port on 127.0.0.1 (0 = ephemeral, default)")
    parser.add_argument("--json", action="store_true",
                        help="print one JSON line with the bound port/url/flag, then serve")
    parser.add_argument("--quiet", action="store_true", help="suppress per-request logging")
    args = parser.parse_args(argv)
    if args.port < 0 or args.port > 65535:
        parser.error("--port must be between 0 and 65535")

    if not args.json:
        print(BANNER, file=sys.stderr)
    # Always warn on stderr: stdout stays one clean JSON line in --json mode.
    print("[demo-target] WARNING: intentionally vulnerable — 127.0.0.1 only, authorized use only.",
          file=sys.stderr, flush=True)

    server = build_server(args.port, quiet=args.quiet)
    actual_port = int(server.server_address[1])
    url = f"http://{HOST}:{actual_port}"
    if not args.json:
        print(f"[demo-target] serving {url}  (Ctrl-C to stop)", file=sys.stderr, flush=True)
    else:
        print(json.dumps({"port": actual_port, "url": url, "flag": FLAG}, ensure_ascii=False),
              flush=True)

    stopping = threading.Event()

    def _stop(signum: int, frame: object) -> None:
        if not stopping.is_set():
            stopping.set()
            threading.Thread(target=server.shutdown, daemon=True).start()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _stop)
        except (ValueError, OSError):  # non-main thread / unsupported platform
            pass

    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            server.server_close()
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
