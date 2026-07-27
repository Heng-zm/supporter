from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import ValidationError

from app.config import Settings
from app.dependencies import get_app_settings, get_telegram_commands
from app.models import TelegramUpdate
from app.services.telegram_commands import TelegramCommandService
from app.utils.security import secure_equals


logger = logging.getLogger("app.telegram_webhook")
router = APIRouter(prefix="/telegram", tags=["telegram"])


@router.post("/webhook", include_in_schema=False)
async def telegram_webhook(
    body: dict[str, Any],
    x_telegram_bot_api_secret_token: str | None = Header(
        default=None,
        alias="X-Telegram-Bot-Api-Secret-Token",
    ),
    settings: Settings = Depends(get_app_settings),
    commands: TelegramCommandService = Depends(get_telegram_commands),
) -> dict[str, bool]:
    if not settings.telegram_commands_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found.")

    if not secure_equals(
        x_telegram_bot_api_secret_token or "",
        settings.telegram_webhook_secret,
    ):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized.")

    try:
        update = TelegramUpdate.model_validate(body)
    except ValidationError:
        return {"ok": True}

    try:
        await commands.handle(update)
    except Exception:
        logger.exception("Telegram update %s failed.", update.update_id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Telegram command processing failed.",
        )
    return {"ok": True}
