from __future__ import annotations

import logging

import httpx
from fastapi import FastAPI

from .config import AudioSettings
from .router import router
from .store import AudioStore
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

    if settings.configuration_error:
        logger.warning("Audio extension initialized with configuration warning: %s", settings.configuration_error)
    else:
        logger.info(
            "Audio extension initialized mode=%s bucket=%s max_bytes=%s",
            store.mode,
            settings.storage_bucket,
            settings.max_bytes,
        )


async def close_audio_extension(app: FastAPI) -> None:
    if getattr(app.state, "audio_owns_http_client", False):
        client = getattr(app.state, "audio_http_client", None)
        if isinstance(client, httpx.AsyncClient):
            await client.aclose()


async def handle_audio_telegram_update(
    app: FastAPI,
    update: dict,
) -> bool:
    controller = getattr(app.state, "audio_telegram", None)
    if not isinstance(controller, TelegramAudioController):
        return False
    return await controller.handle_update(update)
