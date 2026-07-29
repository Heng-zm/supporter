from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx

# Allow direct execution from the project root without installing the app package.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import Settings  # noqa: E402
from app.services.telegram import TelegramService  # noqa: E402


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
