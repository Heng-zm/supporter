from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import re
import time
from collections import OrderedDict
from dataclasses import dataclass

import httpx
from fastapi import APIRouter, FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from app.config import Settings
from app.models import TelegramUpdate
from app.utils.network import client_ip
from app.utils.security import ip_is_trusted, sha256_text

from .integration import handle_audio_telegram_update

logger = logging.getLogger(__name__)

_WEBHOOK_SECRET_RE = re.compile(r"^[A-Za-z0-9_-]{1,256}$")
_MAX_UPDATE_BYTES = 2 * 1024 * 1024
_REPLAY_TTL_SECONDS = 600
_REPLAY_MAX_ITEMS = 10_000

router = APIRouter(prefix="/telegram", tags=["telegram"])


@dataclass(frozen=True, slots=True)
class AudioTelegramWebhookSettings:
    bot_token: str
    secret_token: str
    webhook_url: str
    auto_configure: bool
    drop_pending_updates: bool
    max_connections: int

    @classmethod
    def from_env(cls, *, api_prefix: str = "/api") -> AudioTelegramWebhookSettings:
        explicit_url = os.getenv("TELEGRAM_WEBHOOK_URL", "").strip()
        render_url = os.getenv("RENDER_EXTERNAL_URL", "").strip().rstrip("/")
        normalized_prefix = "/" + api_prefix.strip("/") if api_prefix.strip("/") else ""
        webhook_url = explicit_url or (
            f"{render_url}{normalized_prefix}/telegram/webhook" if render_url else ""
        )

        return cls(
            bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
            secret_token=(
                os.getenv("TELEGRAM_WEBHOOK_SECRET", "").strip()
                or os.getenv("TELEGRAM_WEBHOOK_SECRET_TOKEN", "").strip()
            ),
            webhook_url=webhook_url.rstrip("/"),
            auto_configure=_env_bool("TELEGRAM_AUTO_CONFIGURE_WEBHOOK", False),
            drop_pending_updates=_env_bool(
                "TELEGRAM_WEBHOOK_DROP_PENDING_UPDATES",
                False,
            ),
            max_connections=_env_int(
                "TELEGRAM_WEBHOOK_MAX_CONNECTIONS",
                default=10,
                minimum=1,
                maximum=100,
            ),
        )

    @property
    def receiver_configuration_error(self) -> str:
        if not self.bot_token:
            return "TELEGRAM_BOT_TOKEN is not configured."
        if not self.secret_token:
            return "TELEGRAM_WEBHOOK_SECRET is not configured."
        if not _WEBHOOK_SECRET_RE.fullmatch(self.secret_token):
            return (
                "TELEGRAM_WEBHOOK_SECRET must contain only letters, numbers, "
                "underscore, or hyphen and be 1-256 characters long."
            )
        return ""

    @property
    def configuration_error(self) -> str:
        receiver_error = self.receiver_configuration_error
        if receiver_error:
            return receiver_error
        if not self.webhook_url:
            return (
                "TELEGRAM_WEBHOOK_URL or RENDER_EXTERNAL_URL is not configured."
            )
        if not self.webhook_url.startswith("https://"):
            return "TELEGRAM_WEBHOOK_URL must start with https://."
        return ""


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, *, default: int, minimum: int, maximum: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("Ignoring invalid integer %s=%r", name, raw)
        return default
    return max(minimum, min(maximum, value))


def include_audio_telegram_webhook_router(
    app: FastAPI,
    *,
    api_prefix: str = "/api",
) -> None:
    if getattr(app.state, "audio_telegram_webhook_router_included", False):
        return
    app.include_router(router, prefix=api_prefix.rstrip("/"))
    app.state.audio_telegram_webhook_router_included = True


def _settings(request: Request) -> AudioTelegramWebhookSettings:
    value = getattr(request.app.state, "audio_telegram_webhook_settings", None)
    if isinstance(value, AudioTelegramWebhookSettings):
        return value
    api_prefix = str(getattr(request.app.state, "audio_api_prefix", "/api"))
    value = AudioTelegramWebhookSettings.from_env(api_prefix=api_prefix)
    request.app.state.audio_telegram_webhook_settings = value
    return value


async def _claim_update(app: FastAPI, update_id: int | None) -> str:
    if update_id is None:
        return "untracked"

    lock = getattr(app.state, "audio_telegram_replay_lock", None)
    if not isinstance(lock, asyncio.Lock):
        lock = asyncio.Lock()
        app.state.audio_telegram_replay_lock = lock

    cache = getattr(app.state, "audio_telegram_replay_cache", None)
    if isinstance(cache, dict) and not isinstance(cache, OrderedDict):
        cache = OrderedDict(cache.items())
        app.state.audio_telegram_replay_cache = cache
    elif not isinstance(cache, OrderedDict):
        cache = OrderedDict()
        app.state.audio_telegram_replay_cache = cache

    now = time.monotonic()
    async with lock:
        while cache:
            oldest_key = next(iter(cache))
            _state, timestamp = cache[oldest_key]
            if now - float(timestamp) < _REPLAY_TTL_SECONDS:
                break
            cache.popitem(last=False)

        current = cache.get(update_id)
        if current is not None:
            return str(current[0])

        if len(cache) >= _REPLAY_MAX_ITEMS:
            for _ in range(min(1000, len(cache))):
                cache.popitem(last=False)

        cache[update_id] = ("processing", now)
        return "claimed"


async def _complete_update(app: FastAPI, update_id: int | None) -> None:
    if update_id is None:
        return
    lock = getattr(app.state, "audio_telegram_replay_lock", None)
    cache = getattr(app.state, "audio_telegram_replay_cache", None)
    if not isinstance(lock, asyncio.Lock) or not isinstance(cache, OrderedDict):
        return
    async with lock:
        cache[update_id] = ("completed", time.monotonic())
        cache.move_to_end(update_id)


async def _release_update(app: FastAPI, update_id: int | None) -> None:
    if update_id is None:
        return
    lock = getattr(app.state, "audio_telegram_replay_lock", None)
    cache = getattr(app.state, "audio_telegram_replay_cache", None)
    if not isinstance(lock, asyncio.Lock) or not isinstance(cache, dict):
        return
    async with lock:
        cache.pop(update_id, None)


@router.post("/webhook", include_in_schema=False)
async def audio_telegram_webhook(request: Request) -> JSONResponse:
    audio_settings = _settings(request)
    runtime_settings = getattr(request.app.state, "settings", None)
    commands_enabled = bool(
        isinstance(runtime_settings, Settings)
        and runtime_settings.telegram_commands_enabled
    )

    if audio_settings.receiver_configuration_error and not commands_enabled:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Telegram webhook is not configured.",
        )

    expected_secret = (
        runtime_settings.telegram_webhook_secret
        if commands_enabled
        else audio_settings.secret_token
    )
    received_secret = request.headers.get(
        "X-Telegram-Bot-Api-Secret-Token",
        "",
    ).strip()
    if not hmac.compare_digest(expected_secret, received_secret):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid Telegram webhook secret.",
        )

    if isinstance(runtime_settings, Settings):
        resolved_ip = client_ip(request, runtime_settings)
        allowed_networks = runtime_settings.telegram_webhook_allowed_networks
        if allowed_networks and not ip_is_trusted(resolved_ip, allowed_networks):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Not found.",
            )

        rate_limiter = getattr(
            request.app.state,
            "telegram_webhook_rate_limiter",
            None,
        )
        if rate_limiter is not None:
            rate_key = sha256_text(
                f"{runtime_settings.visit_hash_salt}:telegram-webhook:{resolved_ip}"
            )
            retry_after = await rate_limiter.check(
                rate_key,
                runtime_settings.telegram_webhook_rate_limit_per_minute,
            )
            if retry_after is not None:
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail="Too many webhook requests.",
                    headers={
                        "Retry-After": str(retry_after),
                        "Cache-Control": "no-store",
                    },
                )

    content_type = request.headers.get("content-type", "").lower()
    if "application/json" not in content_type:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="Telegram webhook requires application/json.",
        )

    content_length = request.headers.get("content-length", "").strip()
    if content_length:
        try:
            if int(content_length) > _MAX_UPDATE_BYTES:
                raise HTTPException(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    detail="Telegram update is too large.",
                )
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid Content-Length header.",
            ) from exc

    raw = await request.body()
    if len(raw) > _MAX_UPDATE_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="Telegram update is too large.",
        )

    try:
        update = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Telegram update contains invalid JSON.",
        ) from exc
    if not isinstance(update, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Telegram update must be a JSON object.",
        )

    try:
        update_id = int(update.get("update_id"))
    except (TypeError, ValueError):
        update_id = None

    claim = await _claim_update(request.app, update_id)
    if claim == "completed":
        return JSONResponse({"ok": True, "handled": True, "duplicate": True})
    if claim == "processing":
        response = JSONResponse(
            {"ok": False, "retry": True, "reason": "already_processing"},
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
        response.headers["Retry-After"] = "2"
        return response

    try:
        handled = await handle_audio_telegram_update(request.app, update)
        if not handled and commands_enabled:
            try:
                command_update = TelegramUpdate.model_validate(update)
            except ValidationError:
                command_update = None

            if command_update is not None:
                commands = getattr(request.app.state, "telegram_commands", None)
                if commands is None:
                    raise RuntimeError(
                        "Telegram command service is not initialized."
                    )
                await commands.handle(command_update)
                handled = True
    except Exception:
        await _release_update(request.app, update_id)
        logger.exception("Telegram /audio update processing failed update_id=%s", update_id)
        response = JSONResponse(
            {"ok": False, "retry": True, "reason": "processing_failed"},
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
        response.headers["Retry-After"] = "2"
        return response

    await _complete_update(request.app, update_id)
    return JSONResponse({"ok": True, "handled": bool(handled)})


async def configure_audio_telegram_webhook(
    app: FastAPI,
    *,
    api_prefix: str = "/api",
) -> bool:
    settings = AudioTelegramWebhookSettings.from_env(api_prefix=api_prefix)
    app.state.audio_api_prefix = api_prefix
    app.state.audio_telegram_webhook_settings = settings
    app.state.audio_telegram_webhook_configured = False
    app.state.audio_telegram_webhook_error = ""
    app.state.audio_telegram_webhook_url = settings.webhook_url or None

    if not settings.auto_configure:
        app.state.audio_telegram_webhook_error = (
            "Automatic Telegram webhook configuration is disabled."
        )
        logger.info("Telegram audio webhook auto-configuration is disabled.")
        return False

    if settings.configuration_error:
        app.state.audio_telegram_webhook_error = settings.configuration_error
        logger.warning(
            "Telegram audio webhook configuration skipped: %s",
            settings.configuration_error,
        )
        return False

    client = getattr(app.state, "audio_http_client", None)
    if not isinstance(client, httpx.AsyncClient):
        app.state.audio_telegram_webhook_error = "Audio HTTP client is unavailable."
        logger.error("Telegram audio webhook configuration has no HTTP client.")
        return False

    set_url = f"https://api.telegram.org/bot{settings.bot_token}/setWebhook"
    payload = {
        "url": settings.webhook_url,
        "secret_token": settings.secret_token,
        "allowed_updates": ["message"],
        "drop_pending_updates": settings.drop_pending_updates,
        "max_connections": settings.max_connections,
    }
    runtime_settings = getattr(app.state, "settings", None)
    if (
        isinstance(runtime_settings, Settings)
        and runtime_settings.telegram_commands_enabled
    ):
        payload["allowed_updates"].append("callback_query")

    try:
        response = await client.post(set_url, json=payload)
        data = response.json()
        if response.status_code >= 400 or not isinstance(data, dict) or data.get("ok") is not True:
            app.state.audio_telegram_webhook_error = "Telegram rejected setWebhook."
            logger.error(
                "Telegram setWebhook rejected status=%s",
                response.status_code,
            )
            return False

        info_response = await client.post(
            f"https://api.telegram.org/bot{settings.bot_token}/getWebhookInfo",
            json={},
        )
        info = info_response.json()
        result = info.get("result") if isinstance(info, dict) else None
        actual_url = str(result.get("url") or "") if isinstance(result, dict) else ""
        if (
            info_response.status_code >= 400
            or not isinstance(info, dict)
            or info.get("ok") is not True
            or actual_url.rstrip("/") != settings.webhook_url.rstrip("/")
        ):
            app.state.audio_telegram_webhook_error = (
                "Telegram did not confirm the configured webhook URL."
            )
            logger.error(
                "Telegram getWebhookInfo verification failed status=%s url_matches=%s",
                info_response.status_code,
                actual_url.rstrip("/") == settings.webhook_url.rstrip("/"),
            )
            return False
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        # Do not include the exception text: HTTP client errors can contain the
        # request URL, and Telegram Bot API URLs contain the bot token.
        app.state.audio_telegram_webhook_error = "Telegram API request failed."
        logger.error(
            "Telegram audio webhook configuration failed error_type=%s",
            type(exc).__name__,
        )
        return False

    app.state.audio_telegram_webhook_configured = True
    logger.info("Telegram audio webhook configured url=%s", settings.webhook_url)
    return True
