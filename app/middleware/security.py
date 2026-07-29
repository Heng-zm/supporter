from __future__ import annotations

import logging
import re
import secrets
import time

from starlette.datastructures import MutableHeaders
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.config import Settings
from app.utils.network import request_is_https_scope

logger = logging.getLogger("app.request")
_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{8,64}$")
_HEALTH_PATHS = frozenset({"/health"})


def _header_value(scope: Scope, name: bytes) -> str:
    for key, value in scope.get("headers", []):
        if key.lower() == name:
            return value.decode("latin-1", errors="ignore")
    return ""


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        self.app = app
        self.settings = settings

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        supplied_id = _header_value(scope, b"x-request-id").strip()
        request_id = (
            supplied_id
            if _REQUEST_ID_PATTERN.fullmatch(supplied_id)
            else secrets.token_hex(16)
        )
        state = scope.setdefault("state", {})
        state["request_id"] = request_id
        started_at = time.perf_counter()
        status_code = 500

        async def send_with_request_id(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
                headers = MutableHeaders(scope=message)
                headers["X-Request-ID"] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        finally:
            if self.settings.request_logging_enabled:
                duration_ms = (time.perf_counter() - started_at) * 1000
                logger.info(
                    "request_id=%s method=%s path=%s status=%s duration_ms=%.2f",
                    request_id,
                    scope.get("method", ""),
                    scope.get("path", ""),
                    status_code,
                    duration_ms,
                )


class HTTPSRequiredMiddleware:
    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        self.app = app
        self.settings = settings

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or not self.settings.is_production
            or not self.settings.enforce_https
        ):
            await self.app(scope, receive, send)
            return

        path = str(scope.get("path", ""))
        health_paths = _HEALTH_PATHS | {f"{self.settings.api_prefix}/health"}
        if path in health_paths or request_is_https_scope(scope, self.settings):
            await self.app(scope, receive, send)
            return

        response = JSONResponse(
            status_code=400,
            content={"detail": "HTTPS is required."},
            headers={"Cache-Control": "no-store"},
        )
        await response(scope, receive, send)


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        self.app = app
        self.settings = settings

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not self.settings.security_headers_enabled:
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                if "X-Content-Type-Options" not in headers:
                    headers["X-Content-Type-Options"] = "nosniff"
                if "X-Frame-Options" not in headers:
                    headers["X-Frame-Options"] = "DENY"
                if "Referrer-Policy" not in headers:
                    headers["Referrer-Policy"] = "no-referrer"
                if "Permissions-Policy" not in headers:
                    headers["Permissions-Policy"] = (
                        "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
                    )
                if "X-Permitted-Cross-Domain-Policies" not in headers:
                    headers["X-Permitted-Cross-Domain-Policies"] = "none"
                if "X-Robots-Tag" not in headers:
                    headers["X-Robots-Tag"] = "noindex, nofollow"

                path = str(scope.get("path", ""))
                docs_path = path.startswith(("/docs", "/redoc", "/openapi.json"))
                if not docs_path and "Content-Security-Policy" not in headers:
                    headers["Content-Security-Policy"] = (
                        "default-src 'none'; frame-ancestors 'none'; "
                        "base-uri 'none'; form-action 'none'"
                    )

                if (
                    self.settings.is_production
                    and self.settings.hsts_max_age_seconds > 0
                    and request_is_https_scope(scope, self.settings)
                    and "Strict-Transport-Security" not in headers
                ):
                    value = f"max-age={self.settings.hsts_max_age_seconds}"
                    if self.settings.hsts_include_subdomains:
                        value += "; includeSubDomains"
                    if self.settings.hsts_preload:
                        value += "; preload"
                    headers["Strict-Transport-Security"] = value
            await send(message)

        await self.app(scope, receive, send_with_headers)
