from __future__ import annotations

from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware

from app.config import Settings, get_settings
from app.routers import supporters, visits
from app.services.supabase import SupabaseService
from app.services.telegram import TelegramService
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
        app.state.visits = VisitService(
            runtime_settings,
            app.state.supabase,
            app.state.telegram,
        )
        yield
        await client.aclose()

    app = FastAPI(
        title=runtime_settings.app_name,
        version="1.0.0",
        debug=runtime_settings.debug,
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=runtime_settings.backend_cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Accept", "Content-Type", "X-Admin-Key"],
        max_age=600,
    )
    if runtime_settings.allowed_hosts != ["*"]:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=runtime_settings.allowed_hosts)

    @app.get("/health", tags=["system"])
    async def health() -> dict[str, object]:
        return {
            "ok": True,
            "service": runtime_settings.app_name,
            "environment": runtime_settings.app_environment,
            "supabaseConfigured": runtime_settings.supabase_enabled,
            "telegramConfigured": runtime_settings.telegram_enabled,
        }

    app.include_router(supporters.router, prefix=runtime_settings.api_prefix)
    app.include_router(visits.router, prefix=runtime_settings.api_prefix)
    return app


app = create_app()
