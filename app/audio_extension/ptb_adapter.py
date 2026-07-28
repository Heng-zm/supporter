from __future__ import annotations

from typing import Any

from .telegram import TelegramAudioController


def register_python_telegram_bot_handler(
    application: Any,
    controller: TelegramAudioController,
    *,
    group: int = -90,
) -> None:
    """Register the audio controller in an existing python-telegram-bot app.

    Imports are intentionally local so FastAPI supporter backends that dispatch
    raw Telegram JSON do not need python-telegram-bot installed.
    """

    try:
        from telegram.ext import ApplicationHandlerStop, MessageHandler, filters
    except ImportError as exc:
        raise RuntimeError(
            "python-telegram-bot is not installed; use the raw webhook integration instead."
        ) from exc

    bot_data = getattr(application, "bot_data", None)
    marker = "audio_extension_handler_registered"
    if isinstance(bot_data, dict) and bot_data.get(marker):
        return

    async def handle(update: Any, context: Any) -> None:
        del context
        if update is None or not hasattr(update, "to_dict"):
            return
        payload = update.to_dict()
        if not isinstance(payload, dict):
            return
        if await controller.handle_update(payload):
            raise ApplicationHandlerStop

    application.add_handler(MessageHandler(filters.ALL, handle), group=group)
    if isinstance(bot_data, dict):
        bot_data[marker] = True
