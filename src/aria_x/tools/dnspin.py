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
from collections.abc import Iterator, Mapping

_lock = threading.Lock()
_local = threading.local()
_installed = False
_original_getaddrinfo = socket.getaddrinfo


def _patched_getaddrinfo(host, port, *args, **kwargs):  # type: ignore[no-untyped-def]
    pin = getattr(_local, "pins", None)
    if pin and isinstance(host, str):
        pinned = pin.get(host.lower().rstrip("."))
        if pinned:
            host = pinned
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
def pin_resolution(pins: Mapping[str, str] | None) -> Iterator[None]:
    """Route name lookups for ``pins`` to their recorded address.

    Only the addresses recorded by the scope layer are substituted; every other
    lookup in this thread is untouched. Nested use keeps the outer set.
    """
    if not pins:
        yield
        return
    install()
    previous = getattr(_local, "pins", None)
    merged = dict(previous or {})
    merged.update({k.lower().rstrip("."): v for k, v in pins.items()})
    _local.pins = merged
    try:
        yield
    finally:
        if previous is None:
            try:
                del _local.pins
            except AttributeError:  # pragma: no cover - defensive
                pass
        else:
            _local.pins = previous
