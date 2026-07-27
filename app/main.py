from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, Request, Response, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse

from app.config import Settings, get_settings
from app.middleware.body_limit import RequestBodyLimitMiddleware
from app.middleware.security import (
    HTTPSRequiredMiddleware,
    RequestContextMiddleware,
    SecurityHeadersMiddleware,
)
from app.routers import supporters, telegram, visits
from app.services.rate_limit import TokenBucketRateLimiter
from app.services.supabase import SupabaseService
from app.services.telegram import TelegramService
from app.services.telegram_commands import TelegramCommandService
from app.services.visit_crypto import VisitCryptoService
from app.services.visits import VisitService


APP_VERSION = "1.6.0"
logger = logging.getLogger("app.main")


def create_app(settings: Settings | None = None) -> FastAPI:
    runtime_settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        timeout = httpx.Timeout(
            connect=min(4.0, runtime_settings.request_timeout_seconds),
            read=runtime_settings.request_timeout_seconds,
            write=runtime_settings.request_timeout_seconds,
            pool=min(3.0, runtime_settings.request_timeout_seconds),
        )
        limits = httpx.Limits(
            max_connections=50,
            max_keepalive_connections=20,
            keepalive_expiry=30.0,
        )
        client = httpx.AsyncClient(
            timeout=timeout,
            limits=limits,
            follow_redirects=False,
            trust_env=False,
            headers={"User-Agent": f"ozo-donation-api/{APP_VERSION}"},
        )
        app.state.settings = runtime_settings
        app.state.http_client = client
        app.state.admin_rate_limiter = TokenBucketRateLimiter(max_items=5000)
        app.state.telegram_webhook_rate_limiter = TokenBucketRateLimiter(max_items=5000)
        app.state.supabase = SupabaseService(runtime_settings, client)
        app.state.telegram = TelegramService(runtime_settings, client)
        app.state.visit_crypto = VisitCryptoService(runtime_settings)
        app.state.visits = VisitService(
            runtime_settings,
            app.state.supabase,
            app.state.telegram,
        )
        app.state.telegram_commands = TelegramCommandService(
            runtime_settings,
            app.state.supabase,
            app.state.telegram,
        )

        if runtime_settings.is_production:
            app.state.visit_crypto.ensure_required_encryption_ready()

        if (
            runtime_settings.telegram_commands_enabled
            and runtime_settings.telegram_auto_configure_webhook
        ):
            webhook_result, commands_result = await asyncio.gather(
                app.state.telegram.configure_webhook(),
                app.state.telegram.configure_commands(),
            )
            if not webhook_result.ok:
                logger.error("Telegram webhook configuration failed: %s", webhook_result.error)
            if not commands_result.ok:
                logger.error("Telegram command configuration failed: %s", commands_result.error)

        try:
            yield
        finally:
            await client.aclose()

    docs_enabled = not runtime_settings.is_production or runtime_settings.enable_api_docs
    app = FastAPI(
        title=runtime_settings.app_name,
        version=APP_VERSION,
        debug=runtime_settings.debug,
        lifespan=lifespan,
        docs_url="/docs" if docs_enabled else None,
        redoc_url="/redoc" if docs_enabled else None,
        openapi_url="/openapi.json" if docs_enabled else None,
    )

    app.add_middleware(
        RequestBodyLimitMiddleware,
        max_bytes=runtime_settings.max_request_body_bytes,
    )

    cors_methods = ["GET", "POST", "OPTIONS"]
    cors_headers = ["Accept", "Content-Type", "X-Request-ID"]
    if runtime_settings.admin_cors_enabled:
        cors_methods.extend(["PATCH", "DELETE"])
        cors_headers.append("X-Admin-Key")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=runtime_settings.backend_cors_origins,
        allow_credentials=False,
        allow_methods=cors_methods,
        allow_headers=cors_headers,
        expose_headers=[
            "X-Request-ID",
            "X-Supporters-Source",
            "Warning",
            "Retry-After",
        ],
        max_age=600,
    )
    app.add_middleware(HTTPSRequiredMiddleware, settings=runtime_settings)
    if runtime_settings.allowed_hosts != ["*"]:
        app.add_middleware(
            TrustedHostMiddleware,
            allowed_hosts=runtime_settings.allowed_hosts,
        )
    app.add_middleware(SecurityHeadersMiddleware, settings=runtime_settings)
    app.add_middleware(RequestContextMiddleware, settings=runtime_settings)

    started_at = time.monotonic()

    def health_payload(*, detailed: bool) -> dict[str, object]:
        encryption_ready = bool(
            not runtime_settings.require_encrypted_visits
            or app.state.visit_crypto.enabled
        )
        commands_ready = bool(
            not runtime_settings.telegram_commands_enabled
            or runtime_settings.telegram_commands_configured
        )
        healthy = encryption_ready and commands_ready
        payload: dict[str, object] = {
            "ok": healthy,
            "service": runtime_settings.app_name,
            "version": APP_VERSION,
            "serverTime": datetime.now(timezone.utc).isoformat(),
        }
        if detailed:
            payload.update(
                {
                    "environment": runtime_settings.app_environment,
                    "uptimeSeconds": round(time.monotonic() - started_at, 3),
                    "supabaseConfigured": runtime_settings.supabase_enabled,
                    "telegramVisitAlertsConfigured": (
                        runtime_settings.telegram_visit_alert_enabled
                    ),
                    "telegramCommandsConfigured": (
                        runtime_settings.telegram_commands_configured
                    ),
                    "visitEncryptionConfigured": app.state.visit_crypto.enabled,
                    "supporterAdminApiEnabled": (
                        runtime_settings.supporters_admin_api_enabled
                    ),
                }
            )
            if app.state.visit_crypto.load_error:
                payload["visitEncryptionError"] = app.state.visit_crypto.load_error
        return payload

    def set_health_headers(response: Response) -> None:
        response.headers["Cache-Control"] = "no-store, max-age=0"

    @app.exception_handler(RequestValidationError)
    async def request_validation_error(
        request: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        request_id = getattr(request.state, "request_id", "unknown")
        if runtime_settings.is_production:
            content: dict[str, object] = {
                "detail": "Invalid request.",
                "requestId": request_id,
            }
        else:
            content = {
                "detail": jsonable_encoder(exc.errors()),
                "requestId": request_id,
            }
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content=content,
            headers={"Cache-Control": "no-store"},
        )

    @app.exception_handler(Exception)
    async def unhandled_exception(request: Request, exc: Exception) -> JSONResponse:
        request_id = getattr(request.state, "request_id", "unknown")
        logger.error(
            "Unhandled application error: request_id=%s",
            request_id,
            exc_info=(type(exc), exc, exc.__traceback__),
        )
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "detail": "Internal server error.",
                "requestId": request_id,
            },
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/", tags=["system"], include_in_schema=False)
    async def root(response: Response) -> dict[str, object]:
        set_health_headers(response)
        payload: dict[str, object] = {
            "ok": True,
            "service": runtime_settings.app_name,
            "health": "/health",
        }
        if docs_enabled:
            payload["docs"] = "/docs"
        return payload

    @app.get("/health", tags=["system"], include_in_schema=docs_enabled)
    async def health(response: Response) -> dict[str, object]:
        set_health_headers(response)
        payload = health_payload(detailed=not runtime_settings.is_production)
        if not payload["ok"]:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return payload

    @app.get(f"{runtime_settings.api_prefix}/health", tags=["system"])
    async def api_health(response: Response) -> dict[str, object]:
        set_health_headers(response)
        payload = health_payload(detailed=not runtime_settings.is_production)
        if not payload["ok"]:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return payload

    app.include_router(supporters.router, prefix=runtime_settings.api_prefix)
    app.include_router(visits.router, prefix=runtime_settings.api_prefix)
    app.include_router(telegram.router, prefix=runtime_settings.api_prefix)
    return app


app = create_app()
