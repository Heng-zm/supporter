from __future__ import annotations

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import httpx
import pytest

SCRIPT_PATH = (
    Path(__file__).parents[1] / "scripts" / "check_telegram_webhook.py"
)
SCRIPT_SPEC = spec_from_file_location(
    "project_check_telegram_webhook",
    SCRIPT_PATH,
)
if SCRIPT_SPEC is None or SCRIPT_SPEC.loader is None:
    raise RuntimeError("Could not load the Telegram webhook check script.")
check_telegram_webhook = module_from_spec(SCRIPT_SPEC)
SCRIPT_SPEC.loader.exec_module(check_telegram_webhook)


def _configure_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_COMMANDS_ENABLED", "true")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-bot-token")
    monkeypatch.setenv(
        "TELEGRAM_WEBHOOK_URL",
        "https://api.example.com/api/telegram/webhook",
    )


def _install_transport(
    monkeypatch: pytest.MonkeyPatch,
    *,
    allowed_updates: list[str] | None = None,
    commands: list[str] | None = None,
) -> None:
    original_client = httpx.AsyncClient
    configured_updates = allowed_updates or ["message", "callback_query"]
    configured_commands = commands or ["manage", "command", "commands", "add"]

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/getWebhookInfo"):
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {
                        "url": "https://api.example.com/api/telegram/webhook",
                        "allowed_updates": configured_updates,
                        "pending_update_count": 0,
                    },
                },
            )
        if request.url.path.endswith("/getMyCommands"):
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": [
                        {"command": command, "description": "test"}
                        for command in configured_commands
                    ],
                },
            )
        raise AssertionError(f"Unexpected Telegram method: {request.url.path}")

    def client_factory(*args: object, **kwargs: object) -> httpx.AsyncClient:
        return original_client(
            transport=httpx.MockTransport(handler),
            timeout=kwargs.get("timeout"),
        )

    monkeypatch.setattr(check_telegram_webhook.httpx, "AsyncClient", client_factory)


@pytest.mark.asyncio
async def test_check_telegram_webhook_accepts_complete_configuration(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _configure_environment(monkeypatch)
    _install_transport(monkeypatch)

    assert await check_telegram_webhook.main() == 0
    output = capsys.readouterr()
    assert "configured correctly" in output.out
    assert "test-bot-token" not in output.out


@pytest.mark.asyncio
async def test_check_telegram_webhook_rejects_disabled_commands(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("TELEGRAM_COMMANDS_ENABLED", "false")

    assert await check_telegram_webhook.main() == 2
    assert "must be true" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_check_telegram_webhook_requires_callback_updates(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _configure_environment(monkeypatch)
    _install_transport(monkeypatch, allowed_updates=["message"])

    assert await check_telegram_webhook.main() == 1
    assert "callback_query" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_check_telegram_webhook_requires_add_command(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _configure_environment(monkeypatch)
    _install_transport(monkeypatch, commands=["manage", "command", "commands"])

    assert await check_telegram_webhook.main() == 1
    assert "add" in capsys.readouterr().err
