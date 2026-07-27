from __future__ import annotations

from starlette.requests import Request

from app.config import Settings
from app.models import VisitPayload
from app.services.telegram import TelegramResult
from app.services.visits import VisitService


class FakeSupabase:
    enabled = True

    def __init__(self) -> None:
        self.row: dict[str, object] | None = None
        self.delivery_updates: list[bool] = []

    async def find_recent_visit(self, **_: object) -> dict[str, object] | None:
        return self.row

    async def insert_visit_once(self, row: dict[str, object]) -> dict[str, object]:
        self.row = {
            "id": "visit-1",
            "telegram_sent": False,
            "created_at": "2026-01-01T00:00:00+00:00",
        }
        return self.row

    async def get_visit_by_dedupe_key(self, _: str) -> dict[str, object] | None:
        return self.row

    async def update_visit_delivery(
        self,
        _: str,
        *,
        sent: bool,
        message_id: str | None,
        error: str | None,
    ) -> None:
        self.delivery_updates.append(sent)
        if self.row is not None:
            self.row["telegram_sent"] = sent
            self.row["telegram_message_id"] = message_id
            self.row["telegram_error"] = error


class FlakyTelegram:
    def __init__(self) -> None:
        self.calls = 0

    async def send_visit(self, _: dict[str, object]) -> TelegramResult:
        self.calls += 1
        if self.calls == 1:
            return TelegramResult(ok=False, error="temporary failure")
        return TelegramResult(ok=True, message_id="100")


def make_request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/website/visit",
            "headers": [(b"user-agent", b"Test Browser")],
            "client": ("198.51.100.10", 1234),
            "server": ("test", 80),
            "scheme": "http",
            "query_string": b"",
        }
    )


async def test_telegram_failure_is_retriable_after_storage() -> None:
    settings = Settings(
        supabase_url="https://example.supabase.co",
        supabase_secret_key="secret",
        telegram_bot_token="token",
        telegram_chat_id="123",
        visit_hash_salt="test-salt",
        require_encrypted_visits=False,
        trust_proxy_headers=False,
    )
    supabase = FakeSupabase()
    telegram = FlakyTelegram()
    service = VisitService(settings, supabase, telegram)  # type: ignore[arg-type]
    payload = VisitPayload()

    first = await service.process(make_request(), payload)
    second = await service.process(make_request(), payload)

    assert first.telegram.ok is False
    assert second.duplicate is False
    assert second.telegram.ok is True
    assert telegram.calls == 2
    assert supabase.delivery_updates == [False, True]
