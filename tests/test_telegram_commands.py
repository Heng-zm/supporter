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
        self.markups: list[dict[str, object] | None] = []
        self.edits: list[tuple[str, int, str, dict[str, object] | None]] = []
        self.callback_answers: list[tuple[str, str | None, bool]] = []

    async def send_message(
        self,
        chat_id: str | int,
        text: str,
        *,
        reply_to_message_id: int | None = None,
        reply_markup: dict[str, object] | None = None,
    ) -> object:
        self.messages.append((str(chat_id), text, reply_to_message_id))
        self.markups.append(reply_markup)
        return object()

    async def edit_message_text(
        self,
        chat_id: str | int,
        message_id: int,
        text: str,
        *,
        reply_markup: dict[str, object] | None = None,
    ) -> object:
        self.edits.append((str(chat_id), message_id, text, reply_markup))
        return object()

    async def answer_callback_query(
        self,
        callback_query_id: str,
        text: str | None = None,
        *,
        show_alert: bool = False,
    ) -> object:
        self.callback_answers.append((callback_query_id, text, show_alert))
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


def test_parse_update_fields_supports_partial_patch_and_clear() -> None:
    from app.services.telegram_commands import parse_update_fields

    patch = parse_update_fields(
        "name=Updated User | amount=2,500.75 | message=none | visible=false"
    )
    assert patch.name == "Updated User"
    assert patch.amount == Decimal("2500.75")
    assert patch.message is None
    assert patch.is_visible is False
    assert patch.model_fields_set == {"name", "amount", "message", "is_visible"}


def test_parse_update_fields_rejects_unknown_field() -> None:
    from app.services.telegram_commands import parse_update_fields

    with pytest.raises(ValueError, match="Unknown update field"):
        parse_update_fields("nickname=Test")


class FakeManagerSupabase(FakeCommandSupabase):
    def __init__(self) -> None:
        super().__init__()
        self.supporters: dict[str, dict[str, object]] = {
            "11111111-1111-1111-1111-111111111111": {
                "id": "11111111-1111-1111-1111-111111111111",
                "name": "Alice",
                "amount": 12.5,
                "currency": "USD",
                "message": "Thanks",
                "avatar_url": None,
                "payment_method": "ABA",
                "is_visible": True,
            },
            "22222222-2222-2222-2222-222222222222": {
                "id": "22222222-2222-2222-2222-222222222222",
                "name": "Bob",
                "amount": 5,
                "currency": "USD",
                "message": None,
                "avatar_url": None,
                "payment_method": None,
                "is_visible": False,
            },
        }
        self.updated: list[tuple[str, dict[str, object]]] = []
        self.deleted: list[str] = []

    async def list_supporters_admin(
        self,
        *,
        limit: int,
        offset: int = 0,
    ) -> list[dict[str, object]]:
        rows = list(self.supporters.values())
        return rows[offset : offset + limit]

    async def get_supporter(self, supporter_id: str) -> dict[str, object] | None:
        row = self.supporters.get(supporter_id)
        return dict(row) if row is not None else None

    async def update_supporter(
        self,
        supporter_id: str,
        patch: dict[str, object],
    ) -> dict[str, object] | None:
        row = self.supporters.get(supporter_id)
        if row is None:
            return None
        row.update(patch)
        self.updated.append((supporter_id, patch))
        return dict(row)

    async def delete_supporter(self, supporter_id: str) -> bool:
        self.deleted.append(supporter_id)
        return self.supporters.pop(supporter_id, None) is not None


def _manager_settings():
    from app.config import Settings

    return Settings(
        supabase_url="https://example.supabase.co",
        supabase_secret_key="service-role",
        telegram_bot_token="bot-token",
        telegram_chat_id="123",
        telegram_webhook_secret="abcdefghijklmnopqrstuvwxyz_123456",
        telegram_commands_enabled=True,
        require_encrypted_visits=False,
    )


def _callback_update(update_id: int, data: str, *, user_id: int = 123):
    from app.models import TelegramUpdate

    return TelegramUpdate.model_validate(
        {
            "update_id": update_id,
            "callback_query": {
                "id": f"callback-{update_id}",
                "from": {"id": user_id, "is_bot": False, "first_name": "Admin"},
                "message": {
                    "message_id": 50,
                    "from": {"id": 999, "is_bot": True, "first_name": "Bot"},
                    "chat": {"id": 123, "type": "private"},
                    "text": "Supporter manager",
                },
                "data": data,
            },
        }
    )


async def test_manage_command_shows_add_and_list_buttons() -> None:
    from app.models import TelegramUpdate
    from app.services.telegram_commands import TelegramCommandService

    telegram = FakeCommandTelegram()
    service = TelegramCommandService(
        _manager_settings(),
        FakeManagerSupabase(),
        telegram,  # type: ignore[arg-type]
    )
    update = TelegramUpdate.model_validate(
        {
            "update_id": 2001,
            "message": {
                "message_id": 10,
                "from": {"id": 123, "is_bot": False, "first_name": "Admin"},
                "chat": {"id": 123, "type": "private"},
                "text": "/manage",
            },
        }
    )

    await service.handle(update)

    assert "Supporter manager" in telegram.messages[0][1]
    keyboard = telegram.markups[0]
    assert keyboard is not None
    buttons = keyboard["inline_keyboard"][0]  # type: ignore[index]
    assert {button["callback_data"] for button in buttons} == {"sp:add", "sp:list:0"}


async def test_list_callback_displays_update_and_delete_buttons() -> None:
    from app.services.telegram_commands import TelegramCommandService

    telegram = FakeCommandTelegram()
    service = TelegramCommandService(
        _manager_settings(),
        FakeManagerSupabase(),
        telegram,  # type: ignore[arg-type]
    )

    await service.handle(_callback_update(2002, "sp:list:0"))

    assert telegram.edits
    _, _, text, markup = telegram.edits[0]
    assert "Alice" in text
    assert "Bob" in text
    assert markup is not None
    callback_values = [
        button["callback_data"]
        for row in markup["inline_keyboard"]  # type: ignore[index]
        for button in row
    ]
    assert any(value.startswith("sp:edit:") for value in callback_values)
    assert any(value.startswith("sp:del:") for value in callback_values)


async def test_update_button_then_reply_updates_supporter() -> None:
    from app.models import TelegramUpdate
    from app.services.telegram_commands import TelegramCommandService

    supabase = FakeManagerSupabase()
    telegram = FakeCommandTelegram()
    service = TelegramCommandService(
        _manager_settings(),
        supabase,
        telegram,  # type: ignore[arg-type]
    )
    supporter_id = "11111111-1111-1111-1111-111111111111"

    await service.handle(_callback_update(2003, f"sp:edit:{supporter_id}:0"))
    assert "Update supporter" in telegram.messages[-1][1]

    reply = TelegramUpdate.model_validate(
        {
            "update_id": 2004,
            "message": {
                "message_id": 11,
                "from": {"id": 123, "is_bot": False, "first_name": "Admin"},
                "chat": {"id": 123, "type": "private"},
                "text": "name=Alice Updated | amount=20 | payment=Wing",
            },
        }
    )
    await service.handle(reply)

    assert supabase.updated == [
        (
            supporter_id,
            {"name": "Alice Updated", "amount": 20.0, "payment_method": "Wing"},
        )
    ]
    assert "Supporter updated" in telegram.messages[-1][1]


async def test_delete_requires_confirmation_then_deletes() -> None:
    from app.services.telegram_commands import TelegramCommandService

    supabase = FakeManagerSupabase()
    telegram = FakeCommandTelegram()
    service = TelegramCommandService(
        _manager_settings(),
        supabase,
        telegram,  # type: ignore[arg-type]
    )
    supporter_id = "11111111-1111-1111-1111-111111111111"

    await service.handle(_callback_update(2005, f"sp:del:{supporter_id}:0"))
    assert "Delete supporter?" in telegram.edits[-1][2]
    assert supabase.deleted == []

    await service.handle(_callback_update(2006, f"sp:delok:{supporter_id}:0"))
    assert supabase.deleted == [supporter_id]
    assert all("Alice" not in edit[2] for edit in telegram.edits[-1:])


async def test_unauthorized_callback_does_not_access_database() -> None:
    from app.services.telegram_commands import TelegramCommandService

    supabase = FakeManagerSupabase()
    telegram = FakeCommandTelegram()
    service = TelegramCommandService(
        _manager_settings(),
        supabase,
        telegram,  # type: ignore[arg-type]
    )

    await service.handle(_callback_update(2007, "sp:list:0", user_id=999))

    assert telegram.edits == []
    assert telegram.callback_answers[-1] == ("callback-2007", "You are not authorized.", True)


def _text_update(update_id: int, text: str, *, message_id: int | None = None):
    from app.models import TelegramUpdate

    return TelegramUpdate.model_validate(
        {
            "update_id": update_id,
            "message": {
                "message_id": message_id or update_id,
                "from": {"id": 123, "is_bot": False, "first_name": "Admin"},
                "chat": {"id": 123, "type": "private"},
                "text": text,
            },
        }
    )


async def test_guided_add_asks_one_field_at_a_time_and_saves() -> None:
    from app.services.telegram_commands import TelegramCommandService

    supabase = FakeCommandSupabase()
    telegram = FakeCommandTelegram()
    service = TelegramCommandService(
        _manager_settings(),
        supabase,
        telegram,  # type: ignore[arg-type]
    )

    await service.handle(_text_update(3001, "/add"))
    assert "Step 1/7" in telegram.messages[-1][1]
    assert "Name" in telegram.messages[-1][1]

    await service.handle(_text_update(3002, "Chuo Kimheng"))
    assert "Step 2/7" in telegram.messages[-1][1]

    await service.handle(_text_update(3003, "1.00"))
    assert "Step 3/7" in telegram.messages[-1][1]

    await service.handle(_callback_update(3004, "sp:wizard:value:USD"))
    await service.handle(_callback_update(3005, "sp:wizard:skip"))
    await service.handle(
        _text_update(3006, "https://pay-coffee-topaz.vercel.app/favicon.ico")
    )
    await service.handle(_callback_update(3007, "sp:wizard:value:ABA"))
    await service.handle(_callback_update(3008, "sp:wizard:value:true"))

    assert "Confirm information" in telegram.messages[-1][1]
    await service.handle(_callback_update(3009, "sp:wizard:save"))

    assert len(supabase.calls) == 1
    row, update_id = supabase.calls[0]
    assert update_id == 3009
    assert row == {
        "name": "Chuo Kimheng",
        "amount": 1.0,
        "currency": "USD",
        "message": None,
        "avatar_url": "https://pay-coffee-topaz.vercel.app/favicon.ico",
        "payment_method": "ABA",
        "is_visible": True,
    }
    assert "Supporter added" in telegram.messages[-1][1]


async def test_guided_add_invalid_amount_stays_on_amount_step() -> None:
    from app.services.telegram_commands import TelegramCommandService

    service = TelegramCommandService(
        _manager_settings(),
        FakeCommandSupabase(),
        FakeCommandTelegram(),  # type: ignore[arg-type]
    )
    telegram = service.telegram

    await service.handle(_text_update(3101, "/add"))
    await service.handle(_text_update(3102, "Alice"))
    await service.handle(_text_update(3103, "one dollar"))

    assert "valid en-US number" in telegram.messages[-1][1]  # type: ignore[attr-defined]
    pending = await service.pending_actions.get(123, 123)
    assert pending is not None
    assert pending.step == "amount"


async def test_guided_update_keep_clear_change_and_confirm() -> None:
    from app.services.telegram_commands import TelegramCommandService

    supabase = FakeManagerSupabase()
    telegram = FakeCommandTelegram()
    service = TelegramCommandService(
        _manager_settings(),
        supabase,
        telegram,  # type: ignore[arg-type]
    )
    supporter_id = "11111111-1111-1111-1111-111111111111"

    await service.handle(_callback_update(3201, f"sp:edit:{supporter_id}:0"))
    assert "Step 1/7" in telegram.messages[-1][1]
    assert "Keep current" in telegram.messages[-1][1]

    await service.handle(_callback_update(3202, "sp:wizard:keep"))
    await service.handle(_text_update(3203, "20.00"))
    await service.handle(_callback_update(3204, "sp:wizard:keep"))
    await service.handle(_callback_update(3205, "sp:wizard:clear"))
    await service.handle(_callback_update(3206, "sp:wizard:keep"))
    await service.handle(_text_update(3207, "Wing"))
    await service.handle(_callback_update(3208, "sp:wizard:value:false"))

    assert "Confirm information" in telegram.messages[-1][1]
    await service.handle(_callback_update(3209, "sp:wizard:save"))

    assert supabase.updated == [
        (
            supporter_id,
            {
                "amount": 20.0,
                "message": None,
                "payment_method": "Wing",
                "is_visible": False,
            },
        )
    ]
    assert "Supporter updated" in telegram.messages[-1][1]
