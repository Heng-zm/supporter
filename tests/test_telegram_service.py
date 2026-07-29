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


async def test_webhook_configuration_enables_callback_queries() -> None:
    payloads: list[dict[str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        import json

        payloads.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, json={"ok": True, "result": True})

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
    assert payloads[0]["allowed_updates"] == ["message", "callback_query"]


async def test_command_configuration_registers_supporter_aliases() -> None:
    payloads: list[dict[str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        import json

        payloads.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, json={"ok": True, "result": True})

    settings = Settings(
        telegram_bot_token="token",
        require_encrypted_visits=False,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = TelegramService(settings, client)
        result = await service.configure_commands()

    commands = payloads[0]["commands"]
    assert isinstance(commands, list)
    command_names = {
        str(command["command"])
        for command in commands
        if isinstance(command, dict)
    }
    assert {"add", "command", "commands", "manage"} <= command_names
    assert result.ok is True


async def test_send_message_includes_inline_keyboard() -> None:
    payloads: list[dict[str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        import json

        payloads.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(
            200,
            json={"ok": True, "result": {"message_id": 123}},
        )

    settings = Settings(
        telegram_bot_token="token",
        require_encrypted_visits=False,
    )
    keyboard = {
        "inline_keyboard": [[{"text": "Add", "callback_data": "sp:add"}]]
    }
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = TelegramService(settings, client)
        result = await service.send_message("123", "Menu", reply_markup=keyboard)

    assert result.ok is True
    assert payloads[0]["reply_markup"] == keyboard

async def test_network_error_does_not_leak_bot_token() -> None:
    token = "123456:abcdefghijklmnopqrstuvwxyzABCDE"

    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"failed to connect to {request.url}", request=request)

    settings = Settings(
        telegram_bot_token=token,
        require_encrypted_visits=False,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = TelegramService(settings, client)
        result = await service.send_message("123", "hello")

    assert result.ok is False
    assert result.error == "Unable to reach Telegram."
    assert token not in (result.error or "")
