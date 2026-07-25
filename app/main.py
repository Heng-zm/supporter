from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
import time

import httpx
from fastapi import FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware

from app.config import Settings, get_settings
from app.routers import supporters, visits
from app.services.supabase import SupabaseService
from app.services.telegram import TelegramService
from app.services.visit_crypto import VisitCryptoService
from app.services.visits import VisitService


def create_app(settings: Settings | None = None) -> FastAPI:
    runtime_settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        timeout = httpx.Timeout(runtime_settings.request_timeout_seconds)
        client = httpx.AsyncClient(timeout=timeout, follow_redirects=False)
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
        yield
        await client.aclose()

    app = FastAPI(
        title=runtime_settings.app_name,
        version="1.1.0",
        debug=runtime_settings.debug,
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=runtime_settings.backend_cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Accept", "Content-Type", "X-Admin-Key"],
        expose_headers=["X-Supporters-Source", "Warning"],
        max_age=600,
    )
    if runtime_settings.allowed_hosts != ["*"]:
        app.add_middleware(
            TrustedHostMiddleware,
            allowed_hosts=runtime_settings.allowed_hosts,
        )

    started_at = time.monotonic()

    def health_payload() -> dict[str, object]:
        payload: dict[str, object] = {
            "ok": True,
            "service": runtime_settings.app_name,
            "version": "1.1.0",
            "environment": runtime_settings.app_environment,
            "serverTime": datetime.now(timezone.utc).isoformat(),
            "uptimeSeconds": round(time.monotonic() - started_at, 3),
            "supabaseConfigured": runtime_settings.supabase_enabled,
            "telegramConfigured": runtime_settings.telegram_enabled,
            "visitEncryptionConfigured": app.state.visit_crypto.enabled,
        }
        # Never leak the reason in production; it's only a debugging aid.
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
    return app


app = create_app()