from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from pydantic import ValidationError

from app.config import Settings
from app.dependencies import (
    get_app_settings,
    get_telegram_commands,
    require_json_content_type,
)
from app.models import TelegramUpdate
from app.services.telegram_commands import TelegramCommandService
from app.utils.network import client_ip
from app.utils.security import ip_is_trusted, secure_equals, sha256_text


logger = logging.getLogger("app.telegram_webhook")
router = APIRouter(prefix="/telegram", tags=["telegram"])


@router.post(
    "/webhook",
    include_in_schema=False,
    dependencies=[Depends(require_json_content_type)],
)
async def telegram_webhook(
    request: Request,
    response: Response,
    body: dict[str, Any],
    x_telegram_bot_api_secret_token: str | None = Header(
        default=None,
        alias="X-Telegram-Bot-Api-Secret-Token",
    ),
    settings: Settings = Depends(get_app_settings),
    commands: TelegramCommandService = Depends(get_telegram_commands),
) -> dict[str, bool]:
    response.headers["Cache-Control"] = "no-store, max-age=0"

    if not settings.telegram_commands_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found.")

    resolved_ip = client_ip(request, settings)
    allowed_networks = settings.telegram_webhook_allowed_networks
    if allowed_networks and not ip_is_trusted(resolved_ip, allowed_networks):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found.")

    rate_key = sha256_text(f"{settings.visit_hash_salt}:telegram-webhook:{resolved_ip}")
    retry_after = await request.app.state.telegram_webhook_rate_limiter.check(
        rate_key,
        settings.telegram_webhook_rate_limit_per_minute,
    )
    if retry_after is not None:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many webhook requests.",
            headers={"Retry-After": str(retry_after), "Cache-Control": "no-store"},
        )

    supplied_secret = x_telegram_bot_api_secret_token or ""
    if len(supplied_secret) > 256 or not secure_equals(
        supplied_secret,
        settings.telegram_webhook_secret,
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized.",
            headers={"Cache-Control": "no-store"},
        )

    try:
        update = TelegramUpdate.model_validate(body)
    except ValidationError:
        # Telegram retries non-2xx responses. Ignore unsupported or malformed
        # update types after authentication instead of creating a retry storm.
        return {"ok": True}

    try:
        await commands.handle(update)
    except Exception:
        request_id = getattr(request.state, "request_id", "unknown")
        logger.exception(
            "Telegram update failed: request_id=%s update_id=%s",
            request_id,
            update.update_id,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Telegram command processing failed.",
            headers={"Cache-Control": "no-store"},
        )
    return {"ok": True}
