"""Engagement scope enforcement.

ARIA-X refuses to touch anything that is not an explicit, authorized target. The
scope language is deliberately small and auditable::

    targets = [
      "127.0.0.1",                     # exact host (any port)
      "10.10.0.0/24",                  # CIDR
      "*.staging.example.test",        # domain + all subdomains
      "https://app.example.test:8443/api"  # URL prefix
    ]

Every network-capable tool calls :meth:`Scope.require_url` or
:meth:`Scope.require_host` before the first packet leaves the process.
"""

from __future__ import annotations

import ipaddress
import posixpath
import re
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence
from urllib.parse import unquote, urlsplit

from ..config import ScopeConfig
from ..errors import ScopeViolation

_IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_URL = re.compile(r"https?://[^\s\"'<>]+", re.I)


@dataclass(slots=True)
class Decision:
    allowed: bool
    reason: str
    target: str = ""
    host: str = ""
    port: int | None = None
    ips: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"allowed": self.allowed, "reason": self.reason, "target": self.target,
                "host": self.host, "port": self.port, "ips": self.ips}


@dataclass(slots=True)
class _HostRule:
    host: str
    port: int | None = None
    wildcard: bool = False

    def matches(self, host: str, port: int | None) -> bool:
        host = host.lower().strip(".")
        rule = self.host.lower().strip(".")
        if self.wildcard:
            if not (host == rule or host.endswith("." + rule)):
                return False
        elif host != rule:
            return False
        if self.port is not None and port is not None and int(self.port) != int(port):
            return False
        return True


_ENCODED_TRAVERSAL = re.compile(r"%(?:2e|2f|5c|00)", re.I)
_BAD_PERCENT = re.compile(r"%(?![0-9a-fA-F]{2})")


def _has_control_chars(value: str) -> bool:
    return any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value)


def _validate_scoped_path(raw_path: str) -> tuple[bool, str, str]:
    """Validate a URL path strictly without rewriting ambiguous traversal segments."""
    path = raw_path or "/"
    if not path.startswith("/"):
        return False, "", "URL path must start with '/'"
    if _has_control_chars(path):
        return False, "", "URL path contains control characters"
    if "\\" in path or ";" in path or "//" in path:
        return False, "", "URL path contains ambiguous separator or parameter syntax"
    if _BAD_PERCENT.search(path):
        return False, "", "URL path contains malformed percent-encoding"
    if any(seg in {".", ".."} for seg in path.split("/")):
        return False, "", "URL path contains dot traversal segments"

    cur = path
    for _ in range(5):
        if _ENCODED_TRAVERSAL.search(cur):
            return False, "", "encoded path separator or dot segment not allowed"
        nxt = unquote(cur)
        if _has_control_chars(nxt):
            return False, "", "decoded URL path contains control characters"
        if "\\" in nxt or ";" in nxt or "//" in nxt:
            return False, "", "decoded URL path contains ambiguous separator"
        if any(seg.split(";", 1)[0] in {".", ".."} for seg in nxt.split("/")):
            return False, "", "decoded URL path contains dot traversal segments"
        if nxt == cur:
            break
        cur = nxt
    else:
        return False, "", "URL path exceeds percent-decoding limit"

    normalized = path.rstrip("/") or "/"
    return True, normalized, ""


@dataclass(slots=True)
class _UrlRule:
    scheme: str
    host_rule: _HostRule
    path_prefix: str = ""

    def check(self, scheme: str, host: str, port: int | None, path: str) -> tuple[bool, str]:
        if self.scheme and self.scheme != scheme:
            return False, ""
        if not self.host_rule.matches(host, port):
            return False, ""
        prefix = self.path_prefix.rstrip("/")
        if not prefix:
            return True, ""
        ok, cmp_path, reason = _validate_scoped_path(path or "/")
        if not ok:
            return False, reason
        if cmp_path == prefix or cmp_path.startswith(prefix + "/"):
            return True, ""
        return False, ""

    def matches(self, scheme: str, host: str, port: int | None, path: str) -> bool:
        ok, _ = self.check(scheme, host, port, path)
        return ok


class Scope:
    def __init__(self, config: ScopeConfig, *, allow_public_targets: bool = True) -> None:
        self.config = config
        self.allow_public_targets = bool(allow_public_targets)
        self.host_rules: list[_HostRule] = []
        self.net_rules: list[ipaddress._BaseNetwork] = []
        self.url_rules: list[_UrlRule] = []
        self.deny_hosts: list[_HostRule] = []
        self.deny_nets: list[ipaddress._BaseNetwork] = []
        self._limiter = RateLimiter(max(0.1, float(config.max_rps or 1.0)))
        for pattern in config.targets:
            self._add_target(str(pattern))
        for pattern in config.deny:
            self._add_deny(str(pattern))

    # ------------------------------------------------------------------
    # construction
    # ------------------------------------------------------------------

    @classmethod
    def from_config(cls, config: ScopeConfig) -> "Scope":
        return cls(config)

    def _add_target(self, pattern: str) -> None:
        pattern = pattern.strip()
        if not pattern:
            return
        if "://" in pattern:
            parts = urlsplit(pattern)
            scheme = parts.scheme.lower()
            host = (parts.hostname or "").strip().lower()
            port = parts.port or (443 if scheme == "https" else (80 if scheme == "http" else None))
            clean_host = host.lstrip("*").lstrip(".")
            self.url_rules.append(_UrlRule(
                scheme=scheme,
                host_rule=_HostRule(host=clean_host, port=port, wildcard=host.startswith("*.")),
                path_prefix=parts.path.rstrip("/"),
            ))
            return
        if self._try_network(pattern, self.net_rules):
            return
        host, port = _split_host_port(pattern)
        self.add_host_rule(host, port)

    def _add_deny(self, pattern: str) -> None:
        pattern = pattern.strip()
        if not pattern:
            return
        if "://" in pattern:
            parts = urlsplit(pattern)
            self._add_deny(parts.hostname or "")
            return
        if self._try_network(pattern, self.deny_nets):
            return
        host, port = _split_host_port(pattern)
        self.deny_hosts.append(_HostRule(host=host, port=port, wildcard=host.startswith("*.")))

    def _try_network(self, pattern: str, bucket: list[ipaddress._BaseNetwork]) -> bool:
        if "/" in pattern:
            try:
                bucket.append(ipaddress.ip_network(pattern, strict=False))
                return True
            except ValueError:
                return False
        try:
            ip = ipaddress.ip_address(pattern)
        except ValueError:
            return False
        bucket.append(ipaddress.ip_network(f"{ip}/{ip.max_prefixlen}", strict=False))
        return True

    def add_host_rule(self, host: str, port: int | None = None) -> None:
        host = (host or "").strip().lower()
        if not host:
            return
        self.host_rules.append(_HostRule(host=host.lstrip("*").lstrip("."), port=port,
                                         wildcard=host.startswith("*.")))

    # ------------------------------------------------------------------
    # authorization
    # ------------------------------------------------------------------

    def authorize_url(self, url: str, method: str = "GET") -> Decision:
        raw_url = str(url or "")
        if raw_url != raw_url.strip() or _has_control_chars(raw_url):
            return Decision(False, "URL contains leading/trailing whitespace or control characters", url)
        parts = urlsplit(raw_url)
        if "\\" in (parts.netloc or ""):
            return Decision(False, "URL authority contains invalid backslash", url)
        scheme = parts.scheme.lower()
        if scheme not in {"http", "https"}:
            return Decision(False, f"unsupported scheme '{parts.scheme}'", url)
        host = parts.hostname or ""
        port = parts.port or (443 if scheme == "https" else 80)
        path = parts.path or "/"
        decision = self._check_common(host, port, target=url)
        if not decision.allowed:
            return decision
        if self._matches_host_or_net(decision.host, port, decision.ips):
            return Decision(True, "in scope", url, decision.host, port, decision.ips)
        path_reason = ""
        for rule in self.url_rules:
            matched, rule_reason = rule.check(scheme, decision.host, port, path)
            if matched:
                return Decision(True, "in scope", url, decision.host, port, decision.ips)
            if rule_reason and not path_reason:
                path_reason = rule_reason
        reason = path_reason or (
            "URL is not covered by any scope URL prefix"
            if self.url_rules else "target is not in scope"
        )
        return Decision(False, reason, url, decision.host, port, decision.ips)

    def authorize_host(self, host: str, port: int | None = None, *, target: str = "") -> Decision:
        eff_target = target or (f"{host}:{port}" if port else host)
        decision = self._check_common(host, port, target=eff_target)
        if not decision.allowed:
            return decision
        if self._matches_host_or_net(decision.host, port, decision.ips) or any(
            rule.host_rule.matches(decision.host, port) for rule in self.url_rules
        ):
            return Decision(True, "in scope", eff_target, decision.host, port, decision.ips)
        return Decision(False, "target is not in scope", eff_target, decision.host, port, decision.ips)

    def require_url(self, url: str, method: str = "GET") -> Decision:
        decision = self.authorize_url(url, method)
        if not decision.allowed:
            raise ScopeViolation(decision.reason, target=url, host=decision.host, port=decision.port)
        return decision

    def require_host(self, host: str, port: int | None = None) -> Decision:
        decision = self.authorize_host(host, port)
        if not decision.allowed:
            raise ScopeViolation(decision.reason, target=host, host=host, port=port)
        return decision

    def _check_common(self, host: str, port: int | None, *, target: str) -> Decision:
        host = (host or "").strip().lower().strip(".")
        if not host:
            return Decision(False, "empty target host", target)
        if not self.config.authorized:
            return Decision(False, "scope.authorized is false", target, host, port)
        if not (self.host_rules or self.net_rules or self.url_rules):
            return Decision(False, "no targets configured in scope", target, host, port)

        # Explicit deny wins.
        for rule in self.deny_hosts:
            if rule.matches(host, port):
                return Decision(False, f"host matches deny rule '{rule.host}'", target, host, port)

        ips: list[str] = []
        try:
            ipaddress.ip_address(host)
            ips = [host]
        except ValueError:
            ips = _resolve(host)

        if not self.allow_public_targets and not ips:
            return Decision(
                False,
                f"cannot verify non-public address for unresolved host '{host}'",
                target, host, port, ips,
            )

        for ip_text in ips:
            try:
                ip = ipaddress.ip_address(ip_text)
            except ValueError:
                continue
            for net in self.deny_nets:
                if ip in net:
                    return Decision(False, f"address {ip} matches deny network {net}", target, host, port, ips)
            if ip.is_loopback and not self.config.allow_loopback:
                return Decision(False, f"loopback address {ip} not allowed", target, host, port, ips)
            if (ip.is_private or ip.is_link_local or ip.is_reserved) and not self.config.allow_private \
                    and not ip.is_loopback:
                return Decision(False, f"private address {ip} not allowed", target, host, port, ips)
            if not self.allow_public_targets and ip.is_global:
                return Decision(False, f"public address {ip} not allowed", target, host, port, ips)

        return Decision(True, "", target, host, port, ips)

    def _matches_host_or_net(self, host: str, port: int | None, ips: Sequence[str]) -> bool:
        for rule in self.host_rules:
            if rule.matches(host, port):
                return True
        for ip_text in ips:
            try:
                ip = ipaddress.ip_address(ip_text)
            except ValueError:
                continue
            for net in self.net_rules:
                if ip in net:
                    return True
        return False

    # ------------------------------------------------------------------
    # command heuristic + rate limiting
    # ------------------------------------------------------------------

    def check_command(self, command: str) -> Decision:
        """Best-effort check for explicit out-of-scope targets in a shell command.

        This is a guardrail, not a sandbox: the Docker sandbox is the enforcement
        boundary for terminal work. We only flag literal IPs and URLs, which
        catches the common accidental ``nmap 8.8.8.8`` mistake.
        """
        for url in _URL.findall(command or ""):
            decision = self.authorize_url(url)
            if not decision.allowed:
                return decision
        for match in _IPV4.findall(command or ""):
            try:
                ip = ipaddress.ip_address(match)
            except ValueError:
                continue
            decision = self.authorize_host(str(ip), None, target=str(ip))
            if not decision.allowed:
                return decision
        return Decision(True, "no explicit out-of-scope literal found", command)

    def acquire_slot(self, host: str) -> None:
        self._limiter.acquire(host)

    def summary(self) -> dict[str, Any]:
        return {
            "authorized": self.config.authorized,
            "targets": list(self.config.targets),
            "deny": list(self.config.deny),
            "host_rules": [{"host": r.host, "port": r.port, "wildcard": r.wildcard} for r in self.host_rules],
            "networks": [str(n) for n in self.net_rules],
            "url_rules": [{"scheme": r.scheme, "host": r.host_rule.host, "port": r.host_rule.port,
                           "path": r.path_prefix} for r in self.url_rules],
            "allow_private": self.config.allow_private,
            "allow_loopback": self.config.allow_loopback,
            "allow_public_targets": self.allow_public_targets,
            "max_rps": self.config.max_rps,
        }


class RateLimiter:
    """Per-host token bucket, serialized with one lock."""

    def __init__(self, max_rps: float) -> None:
        self.max_rps = max_rps
        self._lock = threading.Lock()
        self._next: dict[str, float] = {}

    def acquire(self, host: str) -> None:
        if self.max_rps <= 0:
            return
        interval = 1.0 / self.max_rps
        with self._lock:
            now = time.monotonic()
            next_at = self._next.get(host, 0.0)
            wait = max(0.0, next_at - now)
            self._next[host] = max(now, next_at) + interval
        if wait > 0:
            time.sleep(wait)


def _split_host_port(pattern: str) -> tuple[str, int | None]:
    pattern = pattern.strip()
    if pattern.startswith("["):  # [::1]:8080
        end = pattern.find("]")
        host = pattern[1:end] if end != -1 else pattern
        rest = pattern[end + 1:] if end != -1 else ""
        port = int(rest[1:]) if rest.startswith(":") and rest[1:].isdigit() else None
        return host, port
    if pattern.count(":") == 1:
        host, maybe_port = pattern.rsplit(":", 1)
        if maybe_port.isdigit():
            return host, int(maybe_port)
    return pattern, None


def _resolve(host: str) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except OSError:
        return []
    out: list[str] = []
    for info in infos:
        addr = info[4][0]
        if addr not in out:
            out.append(addr)
    return out
