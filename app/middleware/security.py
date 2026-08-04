from __future__ import annotations

import logging
import re
import secrets
import time
from collections.abc import Sequence

from starlette.datastructures import URL, Headers, MutableHeaders
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import RedirectResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.config import Settings
from app.problems import problem_response
from app.utils.network import request_is_https_scope

logger = logging.getLogger("app.request")
_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{8,64}$")
_HEALTH_PATHS = frozenset({"/health", "/health/live", "/health/ready"})


class ProblemCORSMiddleware(CORSMiddleware):
    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        origin = headers.get("origin")
        if origin is None:
            await self.app(scope, receive, send)
            return

        if scope["method"] == "OPTIONS" and "access-control-request-method" in headers:
            response = self.preflight_response(request_headers=headers)
            if response.status_code >= 400:
                response_headers = {
                    key: value
                    for key, value in response.headers.items()
                    if key.lower() not in {"content-length", "content-type"}
                }
                response = problem_response(
                    scope,
                    status_code=400,
                    detail="The CORS preflight request is not allowed.",
                    error_code="cors_preflight_rejected",
                    headers=response_headers,
                )
            await response(scope, receive, send)
            return

        await self.simple_response(scope, receive, send, request_headers=headers)


class ProblemTrustedHostMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        allowed_hosts: Sequence[str] | None = None,
        www_redirect: bool = True,
    ) -> None:
        values = list(allowed_hosts or ["*"])
        for pattern in values:
            if "*" in pattern[1:] or (
                pattern.startswith("*")
                and pattern != "*"
                and not pattern.startswith("*.")
            ):
                raise ValueError("Allowed host wildcards must use the '*.example.com' form.")
        self.app = app
        self.allowed_hosts = values
        self.allow_any = "*" in values
        self.www_redirect = www_redirect

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if self.allow_any or scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        host = headers.get("host", "").split(":")[0]
        is_valid = False
        found_www_redirect = False
        for pattern in self.allowed_hosts:
            if host == pattern or (pattern.startswith("*") and host.endswith(pattern[1:])):
                is_valid = True
                break
            if f"www.{host}" == pattern:
                found_www_redirect = True

        if is_valid:
            await self.app(scope, receive, send)
            return
        if found_www_redirect and self.www_redirect:
            url = URL(scope=scope)
            response = RedirectResponse(url=str(url.replace(netloc=f"www.{url.netloc}")))
        else:
            response = problem_response(
                scope,
                status_code=400,
                detail="The request Host header is not allowed.",
                error_code="invalid_host",
            )
        await response(scope, receive, send)


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
        health_paths = _HEALTH_PATHS | {
            f"{self.settings.api_prefix}/health",
            f"{self.settings.api_prefix}/v1/health",
            f"{self.settings.api_prefix}/v1/health/live",
            f"{self.settings.api_prefix}/v1/health/ready",
        }
        if path in health_paths or request_is_https_scope(scope, self.settings):
            await self.app(scope, receive, send)
            return

        response = problem_response(
            scope,
            status_code=400,
            detail="HTTPS is required.",
            error_code="https_required",
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
                docs_path = (
                    path in {"/docs", "/redoc", "/openapi.json"}
                    or path.startswith(("/docs/", "/redoc/"))
                )
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
