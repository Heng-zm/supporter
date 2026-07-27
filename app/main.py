from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware

from app.config import Settings, get_settings
from app.middleware.body_limit import RequestBodyLimitMiddleware
from app.routers import supporters, telegram, visits
from app.services.supabase import SupabaseService
from app.services.telegram import TelegramService
from app.services.telegram_commands import TelegramCommandService
from app.services.visit_crypto import VisitCryptoService
from app.services.visits import VisitService


APP_VERSION = "1.3.0"
logger = logging.getLogger("app.main")


def create_app(settings: Settings | None = None) -> FastAPI:
    runtime_settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        timeout = httpx.Timeout(runtime_settings.request_timeout_seconds)
        limits = httpx.Limits(
            max_connections=50,
            max_keepalive_connections=20,
            keepalive_expiry=30.0,
        )
        client = httpx.AsyncClient(
            timeout=timeout,
            limits=limits,
            follow_redirects=False,
        )
        app.state.settings = runtime_settings
        app.state.http_client = client
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

    app = FastAPI(
        title=runtime_settings.app_name,
        version=APP_VERSION,
        debug=runtime_settings.debug,
        lifespan=lifespan,
    )

    app.add_middleware(
        RequestBodyLimitMiddleware,
        max_bytes=runtime_settings.max_request_body_bytes,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=runtime_settings.backend_cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=[
            "Accept",
            "Content-Type",
            "X-Admin-Key",
            "X-Telegram-Bot-Api-Secret-Token",
        ],
        expose_headers=["X-Supporters-Source", "Warning", "Retry-After"],
        max_age=600,
    )
    if runtime_settings.allowed_hosts != ["*"]:
        app.add_middleware(
            TrustedHostMiddleware,
            allowed_hosts=runtime_settings.allowed_hosts,
        )

    started_at = time.monotonic()

    def health_payload() -> dict[str, object]:
        encryption_ready = bool(
            not runtime_settings.require_encrypted_visits
            or app.state.visit_crypto.enabled
        )
        commands_ready = bool(
            not runtime_settings.telegram_commands_enabled
            or runtime_settings.telegram_commands_configured
        )
        payload: dict[str, object] = {
            "ok": encryption_ready and commands_ready,
            "service": runtime_settings.app_name,
            "version": APP_VERSION,
            "environment": runtime_settings.app_environment,
            "serverTime": datetime.now(timezone.utc).isoformat(),
            "uptimeSeconds": round(time.monotonic() - started_at, 3),
            "supabaseConfigured": runtime_settings.supabase_enabled,
            "telegramVisitAlertsConfigured": runtime_settings.telegram_visit_alert_enabled,
            "telegramCommandsConfigured": runtime_settings.telegram_commands_configured,
            "visitEncryptionConfigured": app.state.visit_crypto.enabled,
        }
        if not runtime_settings.is_production and app.state.visit_crypto.load_error:
            payload["visitEncryptionError"] = app.state.visit_crypto.load_error
        return payload

    def set_health_headers(response: Response) -> None:
        response.headers["Cache-Control"] = "no-store, max-age=0"
        response.headers["X-Robots-Tag"] = "noindex, nofollow"

    @app.get("/", tags=["system"], include_in_schema=False)
    async def root(response: Response) -> dict[str, object]:
        set_health_headers(response)
        return {
            "ok": True,
            "service": runtime_settings.app_name,
            "health": "/health",
            "docs": "/docs",
        }

    @app.get("/health", tags=["system"], include_in_schema=True)
    async def health(response: Response) -> dict[str, object]:
        set_health_headers(response)
        return health_payload()

    @app.get(f"{runtime_settings.api_prefix}/health", tags=["system"])
    async def api_health(response: Response) -> dict[str, object]:
        set_health_headers(response)
        return health_payload()

    app.include_router(supporters.router, prefix=runtime_settings.api_prefix)
    app.include_router(visits.router, prefix=runtime_settings.api_prefix)
    app.include_router(telegram.router, prefix=runtime_settings.api_prefix)
    return app


app = create_app()
