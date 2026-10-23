"""Network tools: HTTP, DNS, TCP ports, TLS and web probing.

Every target-touching tool follows the same pipeline:

1. **authorize** with :class:`Scope` *before* any socket is opened
   (``require_url``/``require_host``); a refusal raises :class:`ScopeViolation`,
   which the registry turns into a failed observation and an audit entry;
2. **rate-limit per host** through ``Scope.acquire_slot``;
3. **bound** the request with a timeout, a byte cap and a concurrency cap;
4. **record** the decision and the result in the audit log, and (for HTTP) write
   a redacted JSON evidence file under ``workdir/evidence/``.

``web_search`` is the one deliberate exception: it queries DuckDuckGo for
external research and never sends traffic to an engagement target, so it is not
subject to target scope. It is still audited.
"""

from __future__ import annotations

import json
import re
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from ..util import json_dumps, now_iso, redact, truncate
from .base import ToolContext, ToolRegistry, ToolResult, ToolSpec
from .terminal import audit, clamp_float, clamp_int

USER_AGENT = "ARIA-X/0.1 (authorized security research)"
DEFAULT_HTTP_TIMEOUT = 15.0
MAX_HTTP_BYTES = 2_000_000
MAX_EVIDENCE_EXCERPT = 8000
MAX_HEADER_CHARS = 4000
_SENSITIVE_HEADERS = {"authorization", "cookie", "set-cookie", "proxy-authorization",
                      "x-api-key", "x-auth-token", "www-authenticate"}
_HEADER_NAME = re.compile(r"^[A-Za-z0-9!#$%&'*+\-.^_`|~]+$")
_ALLOWED_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _redact_headers(headers: dict[str, str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in (headers or {}).items():
        text = str(value)
        if str(key).lower() in _SENSITIVE_HEADERS:
            out[str(key)] = "[REDACTED]"
        else:
            out[str(key)] = truncate(redact(text), 600)
    return out


def _clean_headers(raw: Any) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for key, value in raw.items():
        name = str(key).strip()
        text = str(value)
        if not name or not _HEADER_NAME.match(name):
            continue
        if "\r" in text or "\n" in text:
            continue
        if name.lower() in {"host", "content-length", "transfer-encoding", "connection"}:
            continue
        out[name] = text[:2000]
    return out


def _response_headers(resp: Any) -> dict[str, str]:
    collected: dict[str, str] = {}
    try:
        items = list(resp.headers.items())
    except Exception:
        return collected
    for key, value in items:
        name = str(key)
        if name in collected:
            collected[name] = collected[name] + ", " + str(value)
        else:
            collected[name] = str(value)
    trimmed = {k: truncate(v, 1000) for k, v in collected.items()}
    # Keep the observation bounded even for pathological servers.
    while len(json_dumps(trimmed)) > MAX_HEADER_CHARS and trimmed:
        trimmed.popitem()
    return _redact_headers(trimmed)


def _decode_body(raw: bytes, content_type: str) -> str:
    charset = "utf-8"
    match = re.search(r"charset=([\w\-]+)", content_type or "", re.I)
    if match:
        charset = match.group(1)
    try:
        return raw.decode(charset, "replace")
    except LookupError:
        return raw.decode("utf-8", "replace")


def _evidence_dir(ctx: ToolContext) -> Path:
    path = Path(ctx.workdir) / "evidence"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _save_evidence(ctx: ToolContext, payload: dict[str, Any], prefix: str = "http") -> str | None:
    """Persist one redacted JSON evidence record; returns the workdir-relative path."""
    try:
        directory = _evidence_dir(ctx)
        name = f"{prefix}_{time.strftime('%Y%m%d_%H%M%S')}_{abs(hash(json_dumps(payload))) % 10**8:08d}.json"
        target = directory / name
        wrapped = {"kind": prefix, "created_at": now_iso(), "project_id": ctx.project.id,
                   "run_id": ctx.run_id, "task_id": ctx.task_id, "agent": ctx.agent,
                   "data": payload}
        text = truncate(json_dumps(wrapped, indent=2), MAX_EVIDENCE_EXCERPT * 4)
        target.write_text(text, encoding="utf-8")
        try:
            return str(target.relative_to(Path(ctx.workdir).resolve()))
        except ValueError:
            return str(target)
    except OSError:
        return None


class _ScopedRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Follow a redirect only when the *destination* is still authorized.

    urllib follows redirects silently by default; for an engagement tool that
    would let an in-scope server bounce a request onto an out-of-scope host.
    Every hop is re-authorized, re-rate-limited and audited.
    """

    def __init__(self, ctx: ToolContext, log: list[dict[str, Any]]) -> None:
        super().__init__()
        self.ctx = ctx
        self.log = log

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        absolute = urllib.parse.urljoin(req.full_url, newurl)
        decision = self.ctx.scope.require_url(absolute, "GET")
        self.ctx.scope.acquire_slot(decision.host or "")
        # Never forward credentials to a different origin (urllib would).
        if urllib.parse.urlsplit(absolute).netloc != urllib.parse.urlsplit(req.full_url).netloc:
            for header in ("Authorization", "Cookie", "Proxy-Authorization"):
                try:
                    req.remove_header(header)
                except (KeyError, AttributeError):
                    pass
        self.log.append({"from": req.full_url, "to": absolute, "status": int(code)})
        audit(self.ctx, "http_request", "redirect", absolute,
              {"from": req.full_url, "status": int(code), "host": decision.host})
        return super().redirect_request(req, fp, code, msg, headers, absolute)


def _http_request(ctx: ToolContext, *, method: str, url: str, headers: dict[str, str] | None,
                  body: str | None, timeout: float, max_bytes: int,
                  save_evidence: bool) -> ToolResult:
    method = method.upper()
    if method not in _ALLOWED_METHODS:
        return ToolResult.failure(f"unsupported HTTP method '{method}'",
                                  data={"allowed": sorted(_ALLOWED_METHODS)})
    # Scope is checked BEFORE any I/O. Raises ScopeViolation on refusal.
    decision = ctx.scope.require_url(url, method)
    host = decision.host or (urllib.parse.urlsplit(url).hostname or "")
    ctx.scope.acquire_slot(host)
    audit(ctx, "http_request", "allowed", url,
          {"method": method, "host": host, "port": decision.port, "ips": decision.ips})

    request_headers = {"User-Agent": USER_AGENT, "Accept": "*/*", "Accept-Encoding": "identity"}
    request_headers.update(_clean_headers(headers))
    data: bytes | None = None
    if body is not None and method not in {"GET", "HEAD"}:
        data = str(body).encode("utf-8")
        if not any(k.lower() == "content-type" for k in request_headers):
            request_headers["Content-Type"] = "text/plain; charset=utf-8"

    started = time.monotonic()
    status = 0
    response_headers: dict[str, str] = {}
    raw = b""
    final_url = url
    redirects: list[dict[str, Any]] = []
    try:
        request = urllib.request.Request(url, data=data, headers=request_headers, method=method)
        # No environment proxies, and redirects are scope-checked hop by hop.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                             _ScopedRedirectHandler(ctx, redirects))
        try:
            resp = opener.open(request, timeout=timeout)
        except urllib.error.HTTPError as exc:  # non-2xx is still a usable observation
            resp = exc
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            audit(ctx, "http_request", "error", url, {"error": str(reason)})
            return ToolResult.failure(f"request failed: {reason}", data={"url": url, "method": method})
        except (socket.timeout, TimeoutError):
            audit(ctx, "http_request", "error", url, {"error": "timeout"})
            return ToolResult.failure(f"request timed out after {timeout:.1f}s",
                                      data={"url": url, "method": method})
        except (ssl.SSLError, OSError) as exc:
            audit(ctx, "http_request", "error", url, {"error": str(exc)})
            return ToolResult.failure(f"request failed: {exc}", data={"url": url, "method": method})
        with resp:
            status = int(getattr(resp, "status", 0) or getattr(resp, "code", 0) or 0)
            final_url = getattr(resp, "geturl", lambda: url)()
            response_headers = _response_headers(resp)
            content_type = response_headers.get("Content-Type", "")
            try:
                raw = resp.read(max_bytes + 1)
            except (OSError, socket.timeout) as exc:
                audit(ctx, "http_request", "error", url, {"error": f"body read: {exc}"})
                return ToolResult.failure(f"failed reading response body: {exc}",
                                          data={"url": url, "status": status})
    except (ValueError, UnicodeError) as exc:
        return ToolResult.failure(f"invalid request: {exc}", data={"url": url, "method": method})

    elapsed_ms = int((time.monotonic() - started) * 1000)
    truncated = len(raw) > max_bytes
    text_body = _decode_body(raw[:max_bytes], response_headers.get("Content-Type", ""))
    excerpt = truncate(text_body, MAX_EVIDENCE_EXCERPT)
    headers_lower = {k.lower(): v for k, v in response_headers.items()}

    payload: dict[str, Any] = {
        "url": url,
        "final_url": final_url,
        "method": method,
        "status": status,
        "reason": "ok" if 200 <= status < 300 else ("redirect" if 300 <= status < 400 else "error"),
        "ok": 200 <= status < 300,
        "headers": response_headers,
        "body_excerpt": excerpt,
        "body_length": len(raw),
        "truncated": truncated,
        "elapsed_ms": elapsed_ms,
        "server": headers_lower.get("server", ""),
        "content_type": headers_lower.get("content-type", ""),
        "redirect": headers_lower.get("location", ""),
        "redirects": redirects,
    }
    if save_evidence:
        evidence_path = _save_evidence(ctx, {
            "request": {"url": url, "method": method,
                        "headers": _redact_headers(request_headers),
                        "body_excerpt": truncate(redact(str(body or "")), 2000)},
            "response": {"status": status, "headers": response_headers,
                         "body_excerpt": truncate(redact(excerpt), MAX_EVIDENCE_EXCERPT),
                         "length": len(raw), "truncated": truncated},
            "scope": decision.to_dict(),
            "redirects": redirects,
            "elapsed_ms": elapsed_ms,
        }, prefix="http")
        if evidence_path:
            payload["evidence_path"] = evidence_path
    audit(ctx, "http_request", "completed", url,
          {"status": status, "bytes": len(raw), "elapsed_ms": elapsed_ms, "truncated": truncated})
    text = (f"HTTP {status} {method} {final_url} ({len(raw)} bytes, {elapsed_ms} ms)\n"
            f"content-type: {payload['content_type'] or 'n/a'}\n\n{excerpt}")
    return ToolResult.success(payload, text=truncate(text, 10000),
                              artifacts=[payload["evidence_path"]] if payload.get("evidence_path") else None)


# ---------------------------------------------------------------------------
# tools
# ---------------------------------------------------------------------------


def http_request(ctx: ToolContext, kwargs: dict[str, Any]) -> ToolResult:
    url = str(kwargs.get("url") or "").strip()
    if not url:
        return ToolResult.failure("url is required")
    method = str(kwargs.get("method") or "GET").upper()
    timeout = clamp_float(kwargs.get("timeout"), 1.0, 90.0, DEFAULT_HTTP_TIMEOUT)
    max_bytes = clamp_int(kwargs.get("max_bytes"), 256, MAX_HTTP_BYTES, 65536)
    headers = kwargs.get("headers") if isinstance(kwargs.get("headers"), dict) else {}
    body = kwargs.get("body")
    return _http_request(ctx, method=method, url=url, headers=headers, body=body,
                         timeout=timeout, max_bytes=max_bytes, save_evidence=True)


def dns_lookup(ctx: ToolContext, kwargs: dict[str, Any]) -> ToolResult:
    host = str(kwargs.get("host") or "").strip()
    record_type = str(kwargs.get("record_type") or "A").upper().strip()
    if not host:
        return ToolResult.failure("host is required")
    if record_type not in {"A", "AAAA", "PTR", "ANY"}:
        return ToolResult.failure("record_type must be one of A, AAAA, PTR, ANY",
                                  data={"record_type": record_type})
    ctx.scope.require_host(host)
    ctx.scope.acquire_slot(host)
    started = time.monotonic()
    if record_type == "PTR":
        try:
            name, aliases, addresses = socket.gethostbyaddr(host)
        except (OSError, socket.herror, socket.gaierror) as exc:
            audit(ctx, "dns_lookup", "error", host, {"record_type": record_type, "error": str(exc)})
            return ToolResult.success({"host": host, "record_type": "PTR", "records": [],
                                       "error": str(exc), "elapsed_ms": int((time.monotonic() - started) * 1000)},
                                      text=f"PTR lookup failed: {exc}")
        records = [{"name": name, "aliases": list(aliases), "addresses": list(addresses)}]
    else:
        family = {"A": socket.AF_INET, "AAAA": socket.AF_INET6}.get(record_type, socket.AF_UNSPEC)
        try:
            infos = socket.getaddrinfo(host, None, family, socket.SOCK_STREAM)
        except (OSError, socket.gaierror) as exc:
            audit(ctx, "dns_lookup", "error", host, {"record_type": record_type, "error": str(exc)})
            return ToolResult.success({"host": host, "record_type": record_type, "records": [],
                                       "error": str(exc), "elapsed_ms": int((time.monotonic() - started) * 1000)},
                                      text=f"DNS lookup failed: {exc}")
        seen: list[str] = []
        for info in infos:
            addr = info[4][0]
            if addr not in seen:
                seen.append(addr)
        records = [{"address": addr, "family": "IPv6" if ":" in addr else "IPv4"} for addr in seen]
    elapsed_ms = int((time.monotonic() - started) * 1000)
    audit(ctx, "dns_lookup", "completed", host, {"record_type": record_type, "records": len(records)})
    return ToolResult.success({"host": host, "record_type": record_type, "records": records,
                               "elapsed_ms": elapsed_ms},
                              text=json_dumps(records))


def _parse_ports(text: Any, max_ports: int, default_ports: list[int]) -> list[int]:
    raw = str(text or "").strip()
    if not raw:
        return sorted({int(p) for p in default_ports if 0 < int(p) < 65536})[:max_ports]
    ports: set[int] = set()
    for chunk in re.split(r"[,\s]+", raw):
        if not chunk:
            continue
        if "-" in chunk:
            lo_text, _, hi_text = chunk.partition("-")
            try:
                lo, hi = int(lo_text), int(hi_text)
            except ValueError:
                continue
            lo, hi = max(1, min(lo, hi)), min(65535, max(lo, hi))
            for port in range(lo, hi + 1):
                ports.add(port)
                if len(ports) >= max_ports:
                    break
        else:
            try:
                port = int(chunk)
            except ValueError:
                continue
            if 0 < port < 65536:
                ports.add(port)
        if len(ports) >= max_ports:
            break
    return sorted(ports)[:max_ports]


def port_scan(ctx: ToolContext, kwargs: dict[str, Any]) -> ToolResult:
    host = str(kwargs.get("host") or "").strip()
    if not host:
        return ToolResult.failure("host is required")
    ctx.scope.require_host(host)
    max_ports = clamp_int(kwargs.get("max_ports"), 1, int(ctx.config.scope.max_ports or 200), 64)
    default_ports = list(ctx.config.scope.ports or [80, 443])
    ports = _parse_ports(kwargs.get("ports"), max_ports, default_ports)
    if not ports:
        return ToolResult.failure("no valid ports to scan")
    timeout = clamp_float(kwargs.get("timeout"), 0.2, 10.0, 1.0)
    want_banners = bool(kwargs.get("banners", False))
    started = time.monotonic()

    try:
        infos = socket.getaddrinfo(host, ports[0], socket.AF_UNSPEC, socket.SOCK_STREAM)
        family = infos[0][0] if infos else socket.AF_INET
    except (OSError, socket.gaierror) as exc:
        audit(ctx, "port_scan", "error", host, {"error": str(exc)})
        return ToolResult.failure(f"cannot resolve {host}: {exc}")

    def probe(port: int) -> dict[str, Any] | None:
        ctx.scope.acquire_slot(host)
        sock = socket.socket(family, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        try:
            if sock.connect_ex((host, port)) != 0:
                return None
            banner = ""
            if want_banners:
                try:
                    sock.settimeout(min(0.6, timeout))
                    banner = sock.recv(256).decode("utf-8", "replace").strip()
                except OSError:
                    banner = ""
            entry: dict[str, Any] = {"port": port}
            if banner:
                entry["banner"] = truncate(redact(banner), 300)
            return entry
        except OSError:
            return None
        finally:
            sock.close()

    workers = min(32, max(1, len(ports)))
    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(probe, ports))
    except Exception as exc:  # pragma: no cover - defensive
        return ToolResult.failure(f"port scan failed: {exc}")
    open_ports = [r for r in results if r]
    open_ports.sort(key=lambda item: item["port"])
    elapsed_ms = int((time.monotonic() - started) * 1000)
    audit(ctx, "port_scan", "completed", host,
          {"scanned": len(ports), "open": len(open_ports), "elapsed_ms": elapsed_ms})
    data = {"host": host, "scanned": len(ports), "open_count": len(open_ports),
            "open_ports": open_ports, "elapsed_ms": elapsed_ms}
    return ToolResult.success(data, text=json_dumps(data))


def tls_probe(ctx: ToolContext, kwargs: dict[str, Any]) -> ToolResult:
    host = str(kwargs.get("host") or "").strip()
    if not host:
        return ToolResult.failure("host is required")
    port = clamp_int(kwargs.get("port"), 1, 65535, 443)
    timeout = clamp_float(kwargs.get("timeout"), 1.0, 30.0, 8.0)
    ctx.scope.require_host(host, port)
    ctx.scope.acquire_slot(host)
    started = time.monotonic()

    def handshake(verify: bool) -> tuple[dict[str, Any], ssl.SSLSocket]:
        context = ssl.create_default_context()
        if not verify:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        raw = socket.create_connection((host, port), timeout=timeout)
        try:
            tls = context.wrap_socket(raw, server_hostname=host)
        except Exception:
            raw.close()
            raise
        return {}, tls

    verified = True
    tls: ssl.SSLSocket | None = None
    error = ""
    try:
        _, tls = handshake(True)
    except (ssl.SSLError, ssl.CertificateError) as exc:
        verified = False
        error = f"{type(exc).__name__}: {exc}"
        try:
            _, tls = handshake(False)
        except (OSError, ssl.SSLError) as exc2:
            error = f"{error}; unverified retry failed: {exc2}"
    except (OSError, socket.timeout) as exc:
        audit(ctx, "tls_probe", "error", host, {"port": port, "error": str(exc)})
        return ToolResult.success({"host": host, "port": port, "handshake_ok": False,
                                   "error": str(exc),
                                   "elapsed_ms": int((time.monotonic() - started) * 1000)},
                                  text=f"TLS handshake failed: {exc}")

    if tls is None:
        audit(ctx, "tls_probe", "error", host, {"port": port, "error": error})
        return ToolResult.success({"host": host, "port": port, "handshake_ok": False, "error": error,
                                   "elapsed_ms": int((time.monotonic() - started) * 1000)},
                                  text=f"TLS handshake failed: {error}")

    try:
        cert = tls.getpeercert() or {}
        cipher = tls.cipher() or ("", "", 0)
        version = tls.version() or ""
    except (ssl.SSLError, OSError) as exc:
        cert, cipher, version = {}, ("", "", 0), ""
        error = error or f"certificate read failed: {exc}"
    finally:
        try:
            tls.close()
        except OSError:
            pass

    def names(rdns: Any) -> list[str]:
        out: list[str] = []
        try:
            for group in rdns or []:
                for key, value in group:
                    out.append(f"{key}={value}")
        except (TypeError, ValueError):
            pass
        return out

    subject = names(cert.get("subject"))
    issuer = names(cert.get("issuer"))
    san: list[str] = []
    for kind, value in cert.get("subjectAltName", ()) or ():
        san.append(f"{kind}:{value}")
    not_before = cert.get("notBefore", "")
    not_after = cert.get("notAfter", "")
    expired = False
    days_remaining: int | None = None
    if not_after:
        try:
            expires = ssl.cert_time_to_seconds(not_after)
            days_remaining = int((expires - time.time()) // 86400)
            expired = days_remaining < 0
        except (ValueError, OverflowError):
            pass
    self_signed = bool(subject) and subject == issuer
    result = {
        "host": host, "port": port, "handshake_ok": True, "verified": verified,
        "subject": subject, "issuer": issuer, "san": san,
        "not_before": not_before, "not_after": not_after,
        "expired": expired, "days_remaining": days_remaining,
        "self_signed": self_signed, "tls_version": version,
        "cipher": cipher[0], "error": error,
        "elapsed_ms": int((time.monotonic() - started) * 1000),
    }
    audit(ctx, "tls_probe", "completed", f"{host}:{port}",
          {"self_signed": self_signed, "expired": expired, "verified": verified})
    return ToolResult.success(result, text=json_dumps(result))


DEFAULT_WEB_PATHS = ["", "robots.txt", "sitemap.xml", ".well-known/security.txt",
                     "login", "admin", "api", "health"]


def web_probe(ctx: ToolContext, kwargs: dict[str, Any]) -> ToolResult:
    base_url = str(kwargs.get("base_url") or "").strip()
    if not base_url:
        return ToolResult.failure("base_url is required")
    raw_paths = kwargs.get("paths")
    if isinstance(raw_paths, str):
        paths = [p.strip() for p in raw_paths.split(",") if p.strip()]
    elif isinstance(raw_paths, (list, tuple)):
        paths = [str(p).strip() for p in raw_paths if str(p).strip()]
    else:
        paths = list(DEFAULT_WEB_PATHS)
    paths = paths[:20]
    if not base_url.endswith("/"):
        base_url = base_url + "/"
    timeout = clamp_float(kwargs.get("timeout"), 1.0, 30.0, 6.0)
    results: list[dict[str, Any]] = []
    started = time.monotonic()
    for path in paths:
        url = urllib.parse.urljoin(base_url, path)
        try:
            ctx.scope.require_url(url, "GET")
        except Exception as exc:  # deny individual path, keep the probe going
            results.append({"path": path or "/", "url": url, "ok": False,
                            "error": f"scope: {getattr(exc, 'message', exc)}"})
            continue
        response = _http_request(ctx, method="GET", url=url, headers={}, body=None,
                                 timeout=timeout, max_bytes=8192, save_evidence=False)
        if not response.ok:
            results.append({"path": path or "/", "url": url, "ok": False, "error": response.error})
            continue
        payload = response.data or {}
        results.append({"path": path or "/", "url": payload.get("final_url", url),
                        "ok": True, "status": payload.get("status"),
                        "length": payload.get("body_length", 0),
                        "content_type": payload.get("content_type", ""),
                        "redirect": payload.get("redirect", "")})
    elapsed_ms = int((time.monotonic() - started) * 1000)
    audit(ctx, "web_probe", "completed", base_url, {"paths": len(paths), "elapsed_ms": elapsed_ms})
    data = {"base_url": base_url, "results": results, "count": len(results), "elapsed_ms": elapsed_ms}
    return ToolResult.success(data, text=json_dumps(data))


# ---------------------------------------------------------------------------
# external research search (NOT target traffic)
# ---------------------------------------------------------------------------


class _DuckParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[dict[str, str]] = []
        self._current: dict[str, str] | None = None
        self._in_title = False
        self._in_snippet = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        data = dict(attrs)
        classes = (data.get("class") or "").split()
        if tag == "a" and "result__a" in classes:
            self._current = {"title": "", "url": data.get("href") or "", "snippet": ""}
            self._in_title = True
        elif "result__snippet" in classes:
            self._in_snippet = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._in_title:
            self._in_title = False
            if self._current is not None:
                self.results.append(self._current)
                self._current = None
        if tag in {"a", "div", "td", "span"} and self._in_snippet:
            self._in_snippet = False

    def handle_data(self, data: str) -> None:
        if self._in_title and self._current is not None:
            self._current["title"] += data
        elif self._in_snippet and self.results:
            self.results[-1]["snippet"] += data


def _normalize_ddg_url(href: str) -> str:
    if not href:
        return ""
    if href.startswith("//"):
        href = "https:" + href
    parts = urllib.parse.urlsplit(href)
    if parts.hostname and parts.hostname.endswith("duckduckgo.com") and parts.path.startswith("/l/"):
        query = urllib.parse.parse_qs(parts.query)
        if query.get("uddg"):
            return urllib.parse.unquote(query["uddg"][0])
    return href


def web_search(ctx: ToolContext, kwargs: dict[str, Any]) -> ToolResult:
    query = str(kwargs.get("query") or "").strip()
    if not query:
        return ToolResult.failure("query is required")
    limit = clamp_int(kwargs.get("limit"), 1, 10, 8)
    url = "https://html.duckduckgo.com/html/?q=" + urllib.parse.quote_plus(query)
    timeout = clamp_float(kwargs.get("timeout"), 3.0, 30.0, 15.0)
    audit(ctx, "web_search", "external", query, {"url": url, "note": "external research; no target scope"})
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            html_text = resp.read(500_000).decode("utf-8", "replace")
    except Exception as exc:
        audit(ctx, "web_search", "error", query, {"error": str(exc)})
        return ToolResult.failure(f"web search unavailable: {type(exc).__name__}: {exc}",
                                  data={"query": query})
    parser = _DuckParser()
    try:
        parser.feed(html_text)
    except Exception:
        pass
    results = []
    for item in parser.results:
        link = _normalize_ddg_url(item.get("url", ""))
        title = redact(" ".join(item.get("title", "").split()))
        snippet = redact(" ".join(item.get("snippet", "").split()))
        if not title or not link.startswith(("http://", "https://")):
            continue
        results.append({"title": truncate(title, 300), "url": link, "snippet": truncate(snippet, 500)})
        if len(results) >= limit:
            break
    audit(ctx, "web_search", "completed", query, {"results": len(results)})
    data = {"query": query, "results": results, "count": len(results)}
    if not results:
        data["note"] = "no parseable results (blocked page or markup change)"
    return ToolResult.success(data, text=json_dumps(data))


# ---------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------


def register(registry: ToolRegistry) -> None:
    registry.add(ToolSpec(
        name="http_request",
        description=(
            "Issue one HTTP request to an in-scope URL and return status, selected response headers, "
            "a body excerpt, length and elapsed time. Non-2xx responses are returned as normal results. "
            "Request/response evidence is written to workdir/evidence/. Secrets are redacted."
        ),
        parameters={
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "method": {"type": "string", "description": "GET/POST/PUT/PATCH/DELETE/HEAD/OPTIONS."},
                "headers": {"type": "object", "additionalProperties": {"type": "string"}},
                "body": {"type": "string"},
                "timeout": {"type": "number"},
                "max_bytes": {"type": "integer"},
            },
            "required": ["url"],
        },
        risk="medium", tags=("network", "http"), requires_scope=True,
    ), http_request)

    registry.add(ToolSpec(
        name="dns_lookup",
        description="Resolve an in-scope host (A/AAAA) or reverse-resolve an address (PTR).",
        parameters={
            "type": "object",
            "properties": {
                "host": {"type": "string"},
                "record_type": {"type": "string", "enum": ["A", "AAAA", "PTR", "ANY"]},
            },
            "required": ["host"],
        },
        risk="low", tags=("network", "dns"), requires_scope=True, read_only=True,
    ), dns_lookup)

    registry.add(ToolSpec(
        name="port_scan",
        description=(
            "TCP connect scan of an in-scope host over a bounded port list (e.g. '22,80,443' or "
            "'1-100'). Concurrency is capped at 32 and every connection is rate-limited per host. "
            "Optionally reads a short service banner."
        ),
        parameters={
            "type": "object",
            "properties": {
                "host": {"type": "string"},
                "ports": {"type": "string"},
                "timeout": {"type": "number"},
                "max_ports": {"type": "integer"},
                "banners": {"type": "boolean"},
            },
            "required": ["host"],
        },
        risk="high", tags=("network", "scan"), requires_scope=True,
    ), port_scan)

    registry.add(ToolSpec(
        name="tls_probe",
        description=(
            "TLS handshake against an in-scope host: returns certificate subject/issuer/SAN, validity "
            "window, expiry, cipher and whether the chain verified or the certificate is self-signed."
        ),
        parameters={
            "type": "object",
            "properties": {
                "host": {"type": "string"},
                "port": {"type": "integer"},
                "timeout": {"type": "number"},
            },
            "required": ["host"],
        },
        risk="medium", tags=("network", "tls"), requires_scope=True, read_only=True,
    ), tls_probe)

    registry.add(ToolSpec(
        name="web_probe",
        description=(
            "GET a small wordlist of paths against an in-scope base URL and return status, length and "
            "content-type for each. Use it to map an exposed surface without a heavy scanner."
        ),
        parameters={
            "type": "object",
            "properties": {
                "base_url": {"type": "string"},
                "paths": {"type": "array", "items": {"type": "string"}},
                "timeout": {"type": "number"},
            },
            "required": ["base_url"],
        },
        risk="medium", tags=("network", "http", "discovery"), requires_scope=True,
    ), web_probe)

    registry.add(ToolSpec(
        name="web_search",
        description=(
            "External research search over DuckDuckGo HTML. This is NOT target traffic; use it to look "
            "up CVEs, product documentation or technology fingerprints. Results are untrusted data."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer"},
                "timeout": {"type": "number"},
            },
            "required": ["query"],
        },
        risk="low", tags=("research", "web"), read_only=True,
    ), web_search)
