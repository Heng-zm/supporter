from __future__ import annotations

import logging
from typing import Any

import httpx
from fastapi import FastAPI

from .config import AudioSettings
from .router import router
from .store import AudioStore, AudioStoreError
from .telegram import TelegramAudioController

logger = logging.getLogger(__name__)


def include_audio_router(app: FastAPI, *, api_prefix: str = "/api") -> None:
    if getattr(app.state, "audio_router_included", False):
        return
    app.include_router(router, prefix=api_prefix.rstrip("/"))
    app.state.audio_router_included = True


async def start_audio_extension(
    app: FastAPI,
    *,
    http_client: httpx.AsyncClient | None = None,
) -> None:
    existing_store = getattr(app.state, "audio_store", None)
    existing_controller = getattr(app.state, "audio_telegram", None)
    if isinstance(existing_store, AudioStore) and isinstance(
        existing_controller,
        TelegramAudioController,
    ):
        logger.debug("Audio extension startup skipped because it is already initialized.")
        return

    settings = AudioSettings.from_env()
    owns_client = http_client is None
    client = http_client or httpx.AsyncClient(
        timeout=httpx.Timeout(
            float(settings.http_timeout_seconds),
            connect=min(10.0, float(settings.http_timeout_seconds)),
        ),
        limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
        follow_redirects=False,
    )

    store = AudioStore(settings, client)
    controller = TelegramAudioController(settings, client, store)

    app.state.audio_settings = settings
    app.state.audio_http_client = client
    app.state.audio_owns_http_client = owns_client
    app.state.audio_store = store
    app.state.audio_telegram = controller
    app.state.audio_storage_ready = False
    app.state.audio_storage_error_code = ""
    app.state.audio_storage_error = ""

    if settings.configuration_error:
        app.state.audio_storage_error_code = "audio_not_configured"
        app.state.audio_storage_error = settings.configuration_error
        logger.warning(
            "Audio extension initialized with configuration warning: %s",
            settings.configuration_error,
        )
        return

    try:
        await store.initialize(force=True)
    except AudioStoreError as exc:
        app.state.audio_storage_error_code = exc.code
        app.state.audio_storage_error = str(exc)
        logger.error(
            "Audio storage initialization failed code=%s message=%s",
            exc.code,
            exc,
        )
    else:
        app.state.audio_storage_ready = True
        logger.info(
            "Audio extension initialized mode=%s bucket=%s max_bytes=%s",
            store.mode,
            settings.storage_bucket,
            settings.max_bytes,
        )


async def close_audio_extension(app: FastAPI) -> None:
    if getattr(app.state, "audio_owns_http_client", False):
        client = getattr(app.state, "audio_http_client", None)
        if isinstance(client, httpx.AsyncClient) and not client.is_closed:
            await client.aclose()

    # Clearing references makes repeated lifespan runs in tests and development
    # deterministic and prevents accidentally reusing a closed HTTP client.
    for name in (
        "audio_http_client",
        "audio_store",
        "audio_telegram",
        "audio_settings",
    ):
        if hasattr(app.state, name):
            delattr(app.state, name)
    app.state.audio_owns_http_client = False
    app.state.audio_storage_ready = False


async def handle_audio_telegram_update(
    app: FastAPI,
    update: dict[str, Any],
) -> bool:
    controller = getattr(app.state, "audio_telegram", None)
    if not isinstance(controller, TelegramAudioController):
        return False
    return await controller.handle_update(update)
