from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
import time

import httpx
from fastapi import FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware

from app.audio_extension import (
    close_audio_extension,
    get_backend_cors_origins,
    include_audio_router,
    start_audio_extension,
)
from app.config import Settings, get_settings
from app.routers import supporters, visits
from app.services.supabase import SupabaseService
from app.services.telegram import TelegramService
from app.services.visit_crypto import VisitCryptoService
from app.services.visits import VisitService


APP_VERSION = "1.1.1-audio"


def _unique_origins(*origin_groups: list[str] | tuple[str, ...]) -> list[str]:
    """Merge CORS origins while preserving order and removing duplicates."""
    merged: list[str] = []
    seen: set[str] = set()

    for group in origin_groups:
        for raw_origin in group:
            origin = str(raw_origin).strip().rstrip("/")
            if origin and origin not in seen:
                seen.add(origin)
                merged.append(origin)

    return merged


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

        try:
            # Audio uploads/downloads use their own client because Telegram and
            # audio files need a longer timeout than normal supporter requests.
            await start_audio_extension(app)
            yield
        finally:
            await close_audio_extension(app)
            await client.aclose()

    app = FastAPI(
        title=runtime_settings.app_name,
        version=APP_VERSION,
        debug=runtime_settings.debug,
        lifespan=lifespan,
    )

    cors_origins = _unique_origins(
        runtime_settings.backend_cors_origins,
        get_backend_cors_origins(),
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=[
            "Accept",
            "Content-Type",
            "X-Admin-Key",
            "X-Telegram-Bot-Api-Secret-Token",
        ],
        expose_headers=[
            "X-Supporters-Source",
            "Warning",
            "ETag",
            "X-Audio-Version",
            "Content-Length",
        ],
        max_age=600,
    )

    if runtime_settings.allowed_hosts != ["*"]:
        app.add_middleware(
            TrustedHostMiddleware,
            allowed_hosts=runtime_settings.allowed_hosts,
        )

    started_at = time.monotonic()

    def health_payload() -> dict[str, object]:
        audio_store = getattr(app.state, "audio_store", None)
        audio_settings = getattr(app.state, "audio_settings", None)

        payload: dict[str, object] = {
            "ok": True,
            "service": runtime_settings.app_name,
            "version": APP_VERSION,
            "environment": runtime_settings.app_environment,
            "serverTime": datetime.now(timezone.utc).isoformat(),
            "uptimeSeconds": round(time.monotonic() - started_at, 3),
            "supabaseConfigured": runtime_settings.supabase_enabled,
            "telegramConfigured": runtime_settings.telegram_enabled,
            "visitEncryptionConfigured": app.state.visit_crypto.enabled,
            "audioRouteConfigured": True,
            "audioExtensionInitialized": audio_store is not None,
            "audioStorageMode": getattr(audio_store, "mode", None),
            "audioConfigurationError": getattr(
                audio_settings,
                "configuration_error",
                "Audio extension has not started.",
            ),
        }

        # Never leak visit-encryption loading details in production.
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
            "audioMetadata": f"{runtime_settings.api_prefix}/audio/metadata",
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

    # This call was missing from the deployed backend. Without it FastAPI
    # correctly returns 404 for /api/audio/metadata.
    include_audio_router(app, api_prefix=runtime_settings.api_prefix)

    return app


app = create_app()
