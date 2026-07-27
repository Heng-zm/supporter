from __future__ import annotations

import httpx

from app.config import Settings
from app.services.telegram import TelegramService


async def test_invalid_success_response_is_not_treated_as_success() -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=["unexpected"])

    settings = Settings(
        telegram_bot_token="token",
        require_encrypted_visits=False,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = TelegramService(settings, client)
        result = await service.send_message("123", "hello")

    assert result.ok is False
    assert result.error == "Telegram returned an invalid response."


async def test_safe_configuration_call_retries_telegram_429(monkeypatch) -> None:
    calls = 0

    async def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                429,
                json={
                    "ok": False,
                    "error_code": 429,
                    "description": "Too Many Requests",
                    "parameters": {"retry_after": 0},
                },
            )
        return httpx.Response(200, json={"ok": True, "result": True})

    async def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr("app.services.telegram.asyncio.sleep", no_sleep)
    settings = Settings(
        telegram_bot_token="token",
        telegram_webhook_url="https://example.com/api/telegram/webhook",
        telegram_webhook_secret="abcdefghijklmnopqrstuvwxyz_123456",
        require_encrypted_visits=False,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = TelegramService(settings, client)
        result = await service.configure_webhook()

    assert result.ok is True
    assert calls == 2
