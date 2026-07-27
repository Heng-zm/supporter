from __future__ import annotations

import asyncio

import httpx

from app.config import Settings
from app.services.telegram import TelegramService


async def main() -> None:
    settings = Settings()
    if not settings.telegram_commands_configured:
        raise SystemExit("Telegram commands are not fully configured in the environment.")
    if not settings.telegram_webhook_url:
        raise SystemExit("TELEGRAM_WEBHOOK_URL is required.")

    async with httpx.AsyncClient(timeout=15.0) as client:
        service = TelegramService(settings, client)
        webhook = await service.configure_webhook()
        commands = await service.configure_commands()

    if not webhook.ok:
        raise SystemExit(f"Webhook setup failed: {webhook.error}")
    if not commands.ok:
        raise SystemExit(f"Command setup failed: {commands.error}")
    print("Telegram webhook and commands configured successfully.")


if __name__ == "__main__":
    asyncio.run(main())
