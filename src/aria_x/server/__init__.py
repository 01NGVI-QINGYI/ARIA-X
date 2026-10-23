"""HTTP control plane and offline web console for ARIA-X.

The public entry points are :func:`create_server` (build a bound, unstarted
``ThreadingHTTPServer``) and :func:`serve` (create collaborators, then run).
Both are deliberately importable without the engine: the CLI and tests can
supply their own engagement, and ``aria_x.server`` must stay cheap to import.
"""

from __future__ import annotations

from .api import Api, RequestError, Response, StreamResponse, response_for_error
from .app import AriaXHTTPServer, AriaXRequestHandler, create_server, serve

__all__ = [
    "Api",
    "AriaXHTTPServer",
    "AriaXRequestHandler",
    "RequestError",
    "Response",
    "StreamResponse",
    "create_server",
    "response_for_error",
    "serve",
]
