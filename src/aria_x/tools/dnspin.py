"""Pin DNS answers so the host that was authorized is the host that is contacted.

``Scope`` resolves a hostname once to decide whether it is in scope. Without
pinning, the client resolves that name *again* when it connects, so a name that
re-resolves differently (DNS rebinding, short TTL, split-horizon) can be
authorized as one address and contacted as another.

The wrapper is thread-local: it only rewrites lookups for hosts registered in
the calling thread's active pin set, so other threads and other code paths keep
normal resolver behaviour. It is installed once, lazily, and stays transparent
when no pin is active.
"""

from __future__ import annotations

import contextlib
import socket
import threading
from collections.abc import Callable, Iterator, Mapping

_lock = threading.Lock()
_local = threading.local()
_installed = False
_original_getaddrinfo = socket.getaddrinfo


def _patched_getaddrinfo(host, port, *args, **kwargs):  # type: ignore[no-untyped-def]
    resolvers = getattr(_local, "resolvers", None)
    if resolvers and isinstance(host, str):
        key = host.lower().rstrip(".")
        for resolver in reversed(resolvers):
            try:
                mapping = resolver()
            except Exception:  # a broken resolver must not break networking
                mapping = None
            pinned = (mapping or {}).get(key) if mapping else None
            if pinned:
                host = pinned
                break
    return _original_getaddrinfo(host, port, *args, **kwargs)


def install() -> None:
    """Install the transparent resolver wrapper (idempotent, process-wide)."""
    global _installed
    with _lock:
        if _installed:
            return
        if socket.getaddrinfo is _patched_getaddrinfo:  # pragma: no cover - defensive
            _installed = True
            return
        socket.getaddrinfo = _patched_getaddrinfo  # type: ignore[assignment]
        _installed = True


@contextlib.contextmanager
def pin_resolution(pins: "Mapping[str, str] | Callable[[], Mapping[str, str]] | None") -> Iterator[None]:
    """Route name lookups to the address the scope authorized.

    ``pins`` may be a mapping or a callable returning one; a callable is
    re-evaluated for every lookup, so an address authorized mid-request (a
    redirect hop) is pinned for the connection that follows it. Only hosts in
    the active mapping are substituted; everything else resolves normally.
    Nested use stacks and restores on exit.
    """
    if pins is None:
        yield
        return
    install()
    resolver = pins if callable(pins) else (lambda: pins)
    stack = getattr(_local, "resolvers", None)
    if stack is None:
        stack = []
        _local.resolvers = stack
    stack.append(resolver)
    try:
        yield
    finally:
        if stack:
            stack.pop()
        if not stack:
            try:
                del _local.resolvers
            except AttributeError:  # pragma: no cover - defensive
                pass
