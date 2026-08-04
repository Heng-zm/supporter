from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.audio_extension import (
    close_audio_extension,
    configure_audio_telegram_webhook,
    get_backend_cors_origins,
    include_audio_router,
    include_audio_telegram_webhook_router,
    start_audio_extension,
)
from app.config import Settings, get_settings
from app.landing import build_landing_page
from app.middleware.body_limit import RequestBodyLimitMiddleware
from app.middleware.security import (
    HTTPSRequiredMiddleware,
    ProblemCORSMiddleware,
    ProblemTrustedHostMiddleware,
    RequestContextMiddleware,
    SecurityHeadersMiddleware,
)
from app.problems import (
    PROBLEM_RESPONSES,
    http_exception_handler,
    unhandled_exception_handler,
    validation_exception_handler,
)
from app.routers import supporters, visits
from app.services.rate_limit import TokenBucketRateLimiter
from app.services.supabase import SupabaseService
from app.services.telegram import TelegramService
from app.services.telegram_commands import TelegramCommandService
from app.services.visit_crypto import VisitCryptoService
from app.services.visits import VisitService

APP_VERSION = "2.5.0-audio"
API_VERSION = "v1"
logger = logging.getLogger("app.startup")

OPENAPI_TAGS = [
    {
        "name": "system",
        "description": "Service health, availability, and deployment information.",
    },
    {
        "name": "supporters",
        "description": "Public supporter data and authenticated administration.",
    },
    {
        "name": "visits",
        "description": "Encrypted website-visit processing and deduplication.",
    },
    {
        "name": "audio",
        "description": "Versioned audio metadata and integrity-checked streaming.",
    },
    {
        "name": "telegram",
        "description": "Authenticated Telegram automation webhooks.",
    },
]


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
    versioned_prefix = f"{runtime_settings.api_prefix}/{API_VERSION}"

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        timeout = httpx.Timeout(runtime_settings.request_timeout_seconds)
        client = httpx.AsyncClient(timeout=timeout, follow_redirects=False)

        try:
            app.state.settings = runtime_settings
            app.state.http_client = client
            app.state.supabase = SupabaseService(runtime_settings, client)
            app.state.telegram = TelegramService(runtime_settings, client)
            app.state.visit_crypto = VisitCryptoService(runtime_settings)
            app.state.admin_rate_limiter = TokenBucketRateLimiter()
            app.state.telegram_webhook_rate_limiter = TokenBucketRateLimiter()
            app.state.telegram_commands = TelegramCommandService(
                runtime_settings,
                app.state.supabase,
                app.state.telegram,
            )
            app.state.visits = VisitService(
                runtime_settings,
                app.state.supabase,
                app.state.telegram,
            )

            if (
                runtime_settings.require_encrypted_visits
                and not app.state.visit_crypto.enabled
            ):
                reason = app.state.visit_crypto.load_error or "No private key was loaded."
                raise RuntimeError(
                    f"Encrypted visits are required but unavailable: {reason}"
                )

            # Audio uploads/downloads use their own client because Telegram and
            # audio files need a longer timeout than normal supporter requests.
            await start_audio_extension(app)
            await configure_audio_telegram_webhook(
                app,
                api_prefix=runtime_settings.api_prefix,
            )
            if (
                runtime_settings.telegram_commands_enabled
                and runtime_settings.telegram_auto_configure_webhook
            ):
                commands_result = await app.state.telegram.configure_commands()
                app.state.telegram_commands_configured = commands_result.ok
                app.state.telegram_commands_configuration_error = (
                    commands_result.error or ""
                )
                if not commands_result.ok:
                    logger.warning(
                        "Telegram command menu configuration failed: %s",
                        commands_result.error or "unknown error",
                    )
            yield
        finally:
            await close_audio_extension(app)
            await client.aclose()

    docs_enabled = (
        runtime_settings.enable_api_docs
        and not runtime_settings.is_production
    )
    app = FastAPI(
        title=runtime_settings.app_name,
        summary="Secure donation, visit, and audio backend",
        description=(
            "Production-focused API for supporter data, encrypted website-visit "
            "processing, Telegram automation, and versioned audio delivery."
        ),
        version=APP_VERSION,
        debug=runtime_settings.debug,
        lifespan=lifespan,
        openapi_tags=OPENAPI_TAGS,
        docs_url="/docs" if docs_enabled else None,
        redoc_url="/redoc" if docs_enabled else None,
        openapi_url="/openapi.json" if docs_enabled else None,
        responses=PROBLEM_RESPONSES,
    )
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)

    cors_origins = _unique_origins(
        runtime_settings.backend_cors_origins,
        get_backend_cors_origins(),
    )

    app.add_middleware(
        ProblemCORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=[
            "Accept",
            "Content-Type",
            "X-Admin-Key",
            "X-Telegram-Bot-Api-Secret-Token",
            "If-None-Match",
            "If-Range",
            "Range",
            "X-Request-ID",
        ],
        expose_headers=[
            "X-Supporters-Source",
            "X-Request-ID",
            "Warning",
            "ETag",
            "X-Audio-Version",
            "Content-Length",
            "Content-Range",
            "Accept-Ranges",
        ],
        max_age=600,
    )

    if runtime_settings.allowed_hosts != ["*"]:
        app.add_middleware(
            ProblemTrustedHostMiddleware,
            allowed_hosts=runtime_settings.allowed_hosts,
        )

    app.add_middleware(
        RequestBodyLimitMiddleware,
        max_bytes=runtime_settings.max_request_body_bytes,
    )
    app.add_middleware(HTTPSRequiredMiddleware, settings=runtime_settings)
    app.add_middleware(SecurityHeadersMiddleware, settings=runtime_settings)
    app.add_middleware(RequestContextMiddleware, settings=runtime_settings)

    started_at = time.monotonic()

    def health_payload() -> dict[str, object]:
        audio_store = getattr(app.state, "audio_store", None)
        audio_settings = getattr(app.state, "audio_settings", None)

        audio_storage_error_code = getattr(
            audio_store,
            "storage_error_code",
            getattr(app.state, "audio_storage_error_code", ""),
        ) or None
        audio_webhook_error = getattr(
            app.state,
            "audio_telegram_webhook_error",
            "",
        ) or None

        payload: dict[str, object] = {
            "ok": True,
            "service": runtime_settings.app_name,
            "version": APP_VERSION,
            "serverTime": datetime.now(UTC).isoformat(),
            "uptimeSeconds": round(time.monotonic() - started_at, 3),
            "audioRouteConfigured": True,
            "audioTelegramWebhookRouteConfigured": True,
            "audioTelegramWebhookConfigured": bool(
                getattr(app.state, "audio_telegram_webhook_configured", False)
            ),
            "audioExtensionInitialized": audio_store is not None,
            "audioStorageMode": getattr(audio_store, "mode", None),
            "audioStorageReady": getattr(audio_store, "storage_ready", False),
            "audioEncryptionEnabled": bool(
                getattr(audio_settings, "encryption_enabled", False)
            ),
            "audioEncryptionActiveKeyVersion": getattr(
                audio_settings, "encryption_active_key_version", None
            ),
            "audioStorageErrorCode": audio_storage_error_code,
            "audioConfigurationValid": not bool(
                getattr(
                    audio_settings,
                    "configuration_error",
                    "Audio extension has not started.",
                )
            ),
            "audioTelegramWebhookErrorCode": (
                "telegram_webhook_configuration_failed"
                if audio_webhook_error
                else None
            ),
        }

        # Detailed infrastructure errors can reveal deployment structure. Keep
        # production health output safe while preserving full diagnostics in
        # development and test environments.
        if not runtime_settings.is_production:
            payload["environment"] = runtime_settings.app_environment
            payload["supabaseConfigured"] = runtime_settings.supabase_enabled
            payload["telegramConfigured"] = runtime_settings.telegram_enabled
            payload["telegramBotConfigured"] = (
                runtime_settings.telegram_bot_enabled
            )
            payload["telegramCommandsEnabled"] = (
                runtime_settings.telegram_commands_enabled
            )
            payload["telegramCommandsConfigured"] = (
                runtime_settings.telegram_commands_configured
            )
            payload["telegramWebhookAutoConfigureEnabled"] = (
                runtime_settings.telegram_auto_configure_webhook
            )
            payload["telegramCommandMenuConfigured"] = getattr(
                app.state,
                "telegram_commands_configured",
                None,
            )
            payload["telegramCommandMenuConfigurationError"] = getattr(
                app.state,
                "telegram_commands_configuration_error",
                None,
            )
            payload["visitEncryptionConfigured"] = app.state.visit_crypto.enabled
            payload["audioTelegramWebhookURL"] = getattr(
                app.state, "audio_telegram_webhook_url", None
            )
            payload["audioTelegramWebhookError"] = audio_webhook_error
            payload["audioStorageError"] = getattr(
                audio_store,
                "storage_error_message",
                getattr(app.state, "audio_storage_error", ""),
            ) or None
            payload["audioConfigurationError"] = getattr(
                audio_settings,
                "configuration_error",
                "Audio extension has not started.",
            ) or None
            if app.state.visit_crypto.load_error:
                payload["visitEncryptionError"] = app.state.visit_crypto.load_error

        return payload

    def readiness_payload() -> tuple[bool, dict[str, object]]:
        supabase = getattr(app.state, "supabase", None)
        visit_crypto = getattr(app.state, "visit_crypto", None)
        audio_store = getattr(app.state, "audio_store", None)
        audio_settings = getattr(app.state, "audio_settings", None)
        audio_webhook_settings = getattr(
            app.state,
            "audio_telegram_webhook_settings",
            None,
        )

        audio_enabled = bool(getattr(audio_settings, "enabled", False))
        audio_requires_supabase = bool(
            audio_enabled
            and getattr(audio_settings, "resolved_storage_mode", None) == "supabase"
        )
        supabase_required = bool(
            runtime_settings.is_production
            or runtime_settings.supporters_admin_api_enabled
            or runtime_settings.require_visit_storage
            or runtime_settings.telegram_commands_enabled
            or audio_requires_supabase
        )
        supabase_ok = bool(getattr(supabase, "enabled", False))

        encryption_required = runtime_settings.require_encrypted_visits
        encryption_ok = bool(getattr(visit_crypto, "enabled", False))

        audio_configuration_ok = bool(
            audio_settings is not None
            and not getattr(audio_settings, "configuration_error", "")
        )
        audio_storage_ok = bool(
            audio_store is not None
            and getattr(audio_store, "storage_ready", False)
            and audio_configuration_ok
        )

        telegram_required = bool(
            runtime_settings.telegram_commands_enabled
            or (runtime_settings.is_production and runtime_settings.visit_alert_enabled)
        )
        if runtime_settings.telegram_commands_enabled:
            telegram_ok = runtime_settings.telegram_commands_configured
        elif telegram_required:
            telegram_ok = runtime_settings.telegram_visit_alert_enabled
        else:
            telegram_ok = True
        telegram_webhook_required = bool(
            getattr(audio_webhook_settings, "auto_configure", False)
        )
        telegram_webhook_ok = bool(
            not telegram_webhook_required
            or getattr(app.state, "audio_telegram_webhook_configured", False)
        )

        checks: dict[str, dict[str, bool]] = {
            "supabase": {"ok": supabase_ok, "required": supabase_required},
            "visitEncryption": {
                "ok": encryption_ok,
                "required": encryption_required,
            },
            "audioStorage": {"ok": audio_storage_ok, "required": audio_enabled},
            "telegram": {"ok": telegram_ok, "required": telegram_required},
            "telegramWebhook": {
                "ok": telegram_webhook_ok,
                "required": telegram_webhook_required,
            },
        }
        ready = all(
            not check["required"] or check["ok"]
            for check in checks.values()
        )
        return ready, {
            "ok": ready,
            "status": "ready" if ready else "not_ready",
            "service": runtime_settings.app_name,
            "version": APP_VERSION,
            "checks": checks,
        }

    def set_health_headers(response: Response) -> None:
        response.headers["Cache-Control"] = "no-store, max-age=0"
        response.headers["X-Robots-Tag"] = "noindex, nofollow"

    @app.get(
        "/",
        tags=["system"],
        include_in_schema=False,
        response_model=None,
    )
    async def root(
        request: Request,
        response: Response,
    ) -> dict[str, object] | Response:
        if "text/html" in request.headers.get("accept", "").lower():
            return build_landing_page(
                service_name=runtime_settings.app_name,
                version=APP_VERSION,
                api_prefix=versioned_prefix,
                docs_enabled=docs_enabled,
            )

        set_health_headers(response)
        payload: dict[str, object] = {
            "ok": True,
            "status": "operational",
            "service": runtime_settings.app_name,
            "version": APP_VERSION,
            "health": "/health/ready",
            "apiVersion": API_VERSION,
            "apiBase": f"{runtime_settings.api_prefix}/{API_VERSION}",
            "audioMetadata": (
                f"{runtime_settings.api_prefix}/{API_VERSION}/audio/metadata"
            ),
            "telegramWebhook": f"{runtime_settings.api_prefix}/telegram/webhook",
        }
        if docs_enabled:
            payload["docs"] = "/docs"
        return payload

    @app.get("/health", tags=["system"], include_in_schema=True)
    async def health(response: Response) -> dict[str, object]:
        set_health_headers(response)
        return health_payload()

    @app.get(f"{runtime_settings.api_prefix}/health", tags=["system"])
    async def api_health(response: Response) -> dict[str, object]:
        set_health_headers(response)
        return health_payload()

    @app.get("/health/live", tags=["system"])
    async def health_live(response: Response) -> dict[str, object]:
        set_health_headers(response)
        return {
            "ok": True,
            "status": "alive",
            "service": runtime_settings.app_name,
            "version": APP_VERSION,
            "uptimeSeconds": round(time.monotonic() - started_at, 3),
        }

    @app.get("/health/ready", tags=["system"])
    async def health_ready(response: Response) -> dict[str, object]:
        set_health_headers(response)
        ready, payload = readiness_payload()
        response.status_code = 200 if ready else 503
        return payload

    @app.get(f"{versioned_prefix}/health", tags=["system"])
    async def versioned_health(response: Response) -> dict[str, object]:
        set_health_headers(response)
        return health_payload()

    @app.get(f"{versioned_prefix}/health/live", tags=["system"])
    async def versioned_health_live(response: Response) -> dict[str, object]:
        return await health_live(response)

    @app.get(f"{versioned_prefix}/health/ready", tags=["system"])
    async def versioned_health_ready(response: Response) -> dict[str, object]:
        return await health_ready(response)

    app.include_router(
        supporters.router,
        prefix=runtime_settings.api_prefix,
        include_in_schema=False,
    )
    app.include_router(
        visits.router,
        prefix=runtime_settings.api_prefix,
        include_in_schema=False,
    )
    app.include_router(supporters.router, prefix=versioned_prefix)
    app.include_router(visits.router, prefix=versioned_prefix)

    # This call was missing from the deployed backend. Without it FastAPI
    # correctly returns 404 for /api/audio/metadata.
    include_audio_router(
        app,
        api_prefix=runtime_settings.api_prefix,
        include_in_schema=False,
    )
    include_audio_router(app, api_prefix=versioned_prefix)
    include_audio_telegram_webhook_router(
        app,
        api_prefix=runtime_settings.api_prefix,
    )

    return app


app = create_app()
