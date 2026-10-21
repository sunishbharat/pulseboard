"""Endpoint access key check (ADDENDUM-01 §1A.4, ADR-025).

Every HTTP request must carry ``X-Pulseboard-Access-Key`` matching one of the configured keys,
except the health check, signed export links (a browser cannot add headers; the signature
protects those) and the public Plotly library under ``/static``. Keys are compared in constant
time, and every key is always compared, so timing reveals neither the key nor which one matched.
"""

import hashlib
import hmac
from collections.abc import Sequence

from pydantic import SecretStr
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from pulseboard.hosted.caller import ACCESS_KEY_HEADER
from pulseboard.hosted.links import EXPORTS_PATH

HEALTH_PATH = "/health"
STATIC_PATH = "/static"
"""Public files only (the installed Plotly library)."""
REJECTED_STATUS = 401


def _digest(value: bytes) -> bytes:
    # Equal-length inputs for compare_digest, whatever the key lengths are.
    return hashlib.sha256(value).digest()


class AccessKeyMiddleware:
    """Pure ASGI middleware that rejects HTTP requests without a valid access key."""

    def __init__(self, app: ASGIApp, *, keys: Sequence[SecretStr]) -> None:
        self._app = app
        self._keys = tuple(_digest(k.get_secret_value().encode()) for k in keys)

    def allows(self, presented: bytes | None) -> bool:
        """Whether ``presented`` matches a configured key."""
        if not presented or not self._keys:
            return False
        digest = _digest(presented)
        matched = False
        for key in self._keys:
            matched |= hmac.compare_digest(digest, key)
        return matched

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Pass lifespan events, the health check and signed links; check everything else."""
        if scope["type"] != "http" or _is_open(scope.get("path", "")):
            await self._app(scope, receive, send)
            return
        name = ACCESS_KEY_HEADER.encode()
        presented = next((v for k, v in scope.get("headers", ()) if k.lower() == name), None)
        if self.allows(presented):
            await self._app(scope, receive, send)
            return
        response = JSONResponse(
            {"error": "access_denied", "message": "Missing or invalid X-Pulseboard-Access-Key."},
            status_code=REJECTED_STATUS,
        )
        await response(scope, receive, send)


def _is_open(path: str) -> bool:
    return path == HEALTH_PATH or path.startswith((f"{EXPORTS_PATH}/", f"{STATIC_PATH}/"))
