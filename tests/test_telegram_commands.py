from __future__ import annotations

from decimal import Decimal

import pytest

from app.services.telegram_commands import parse_add_command


def test_parse_minimal_add_command() -> None:
    parsed = parse_add_command("/add John Doe | 25")
    assert parsed.supporter.name == "John Doe"
    assert parsed.supporter.amount == Decimal("25")
    assert parsed.supporter.currency == "USD"


def test_parse_full_add_command() -> None:
    parsed = parse_add_command(
        "/add Jane | 1,250.50 | eur | Thanks | https://example.com/a.jpg | Card"
    )
    assert parsed.supporter.amount == Decimal("1250.50")
    assert parsed.supporter.currency == "EUR"
    assert parsed.supporter.payment_method == "Card"


def test_parse_add_command_accepts_avatar_when_message_is_omitted() -> None:
    parsed = parse_add_command(
        "/add Chuo Kimheng | 1.00 | USD |\n"
        "https://pay-coffee-topaz.vercel.app/favicon.ico | ABA"
    )
    assert parsed.supporter.name == "Chuo Kimheng"
    assert parsed.supporter.amount == Decimal("1.00")
    assert parsed.supporter.currency == "USD"
    assert parsed.supporter.message is None
    assert parsed.supporter.avatar_url == (
        "https://pay-coffee-topaz.vercel.app/favicon.ico"
    )
    assert parsed.supporter.payment_method == "ABA"


def test_parse_add_command_accepts_explicit_empty_message() -> None:
    parsed = parse_add_command(
        "/add Chuo Kimheng | 1.00 | USD | | "
        "https://pay-coffee-topaz.vercel.app/favicon.ico | ABA"
    )
    assert parsed.supporter.message is None
    assert parsed.supporter.avatar_url == (
        "https://pay-coffee-topaz.vercel.app/favicon.ico"
    )
    assert parsed.supporter.payment_method == "ABA"


def test_parse_add_command_requires_separator() -> None:
    with pytest.raises(ValueError, match="Use \\|"):
        parse_add_command("/add John 25")


def test_parse_add_command_rejects_bad_comma_grouping() -> None:
    with pytest.raises(ValueError, match="en-US"):
        parse_add_command("/add John | 1,2,3")


class FakeCommandSupabase:
    enabled = True

    def __init__(self) -> None:
        self.calls: list[tuple[dict[str, object], int]] = []

    async def create_supporter_from_telegram(
        self,
        row: dict[str, object],
        update_id: int,
    ) -> dict[str, object]:
        self.calls.append((row, update_id))
        return {
            "id": "supporter-1",
            "name": row["name"],
            "amount": row["amount"],
            "currency": row["currency"],
        }


class FakeCommandTelegram:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str, int | None]] = []

    async def send_message(
        self,
        chat_id: str | int,
        text: str,
        *,
        reply_to_message_id: int | None = None,
    ) -> object:
        self.messages.append((str(chat_id), text, reply_to_message_id))
        return object()


async def test_command_service_adds_supporter() -> None:
    from app.config import Settings
    from app.models import TelegramUpdate
    from app.services.telegram_commands import TelegramCommandService

    settings = Settings(
        supabase_url="https://example.supabase.co",
        supabase_secret_key="service-role",
        telegram_bot_token="bot-token",
        telegram_chat_id="123",
        telegram_webhook_secret="abcdefghijklmnopqrstuvwxyz_123456",
        telegram_commands_enabled=True,
        require_encrypted_visits=False,
    )
    supabase = FakeCommandSupabase()
    telegram = FakeCommandTelegram()
    service = TelegramCommandService(settings, supabase, telegram)  # type: ignore[arg-type]
    update = TelegramUpdate.model_validate(
        {
            "update_id": 999,
            "message": {
                "message_id": 7,
                "from": {"id": 123, "is_bot": False, "first_name": "Admin"},
                "chat": {"id": 123, "type": "private"},
                "text": "/add Alice | 12.50 | USD | Thanks",
            },
        }
    )

    await service.handle(update)

    assert len(supabase.calls) == 1
    assert supabase.calls[0][1] == 999
    assert supabase.calls[0][0]["name"] == "Alice"
    assert telegram.messages[0][0] == "123"
    assert "Supporter added" in telegram.messages[0][1]


def test_parse_add_command_accepts_general_whitespace() -> None:
    parsed = parse_add_command("/add\tAlice | 10.50 | usd")
    assert parsed.supporter.name == "Alice"
    assert parsed.supporter.amount == Decimal("10.50")
    assert parsed.supporter.currency == "USD"


class UnexpectedFailureSupabase(FakeCommandSupabase):
    def __init__(self) -> None:
        super().__init__()
        self.fail_once = True

    async def create_supporter_from_telegram(
        self,
        row: dict[str, object],
        update_id: int,
    ) -> dict[str, object]:
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("unexpected failure")
        return await super().create_supporter_from_telegram(row, update_id)


async def test_unexpected_command_failure_releases_update_for_webhook_retry() -> None:
    from app.config import Settings
    from app.models import TelegramUpdate
    from app.services.telegram_commands import TelegramCommandService

    settings = Settings(
        supabase_url="https://example.supabase.co",
        supabase_secret_key="service-role",
        telegram_bot_token="bot-token",
        telegram_chat_id="123",
        telegram_webhook_secret="abcdefghijklmnopqrstuvwxyz_123456",
        telegram_commands_enabled=True,
        require_encrypted_visits=False,
    )
    supabase = UnexpectedFailureSupabase()
    telegram = FakeCommandTelegram()
    service = TelegramCommandService(settings, supabase, telegram)  # type: ignore[arg-type]
    update = TelegramUpdate.model_validate(
        {
            "update_id": 1001,
            "message": {
                "message_id": 8,
                "from": {"id": 123, "is_bot": False, "first_name": "Admin"},
                "chat": {"id": 123, "type": "private"},
                "text": "/add Retry User | 15.00",
            },
        }
    )

    with pytest.raises(RuntimeError, match="unexpected failure"):
        await service.handle(update)

    await service.handle(update)
    assert len(supabase.calls) == 1
    assert supabase.calls[0][1] == 1001
