from __future__ import annotations

import httpx
import pytest

from app.config import Settings
from app.services.supabase import SupabaseError, SupabaseService
from app.services.telegram_commands import _database_error_message


def _settings() -> Settings:
    return Settings(
        supabase_url="https://project.supabase.co",
        supabase_secret_key="service-role-secret",
        require_encrypted_visits=False,
    )


async def test_create_supporter_sends_valid_post_request() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/rest/v1/supporters"
        assert request.url.params["select"].startswith("id,name,amount")
        return httpx.Response(
            201,
            json=[
                {
                    "id": "supporter-1",
                    "name": "Alice",
                    "amount": 10,
                    "currency": "USD",
                    "message": None,
                    "avatar_url": None,
                    "payment_method": None,
                    "created_at": "2026-07-27T00:00:00Z",
                }
            ],
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = SupabaseService(_settings(), client)
        created = await service.create_supporter(
            {"name": "Alice", "amount": 10, "currency": "USD"}
        )

    assert created["name"] == "Alice"


async def test_supabase_error_preserves_postgrest_code() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "code": "42P10",
                "message": "there is no unique or exclusion constraint matching ON CONFLICT",
                "details": None,
                "hint": None,
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = SupabaseService(_settings(), client)
        with pytest.raises(SupabaseError) as caught:
            await service.create_supporter_from_telegram(
                {"name": "Alice", "amount": 10, "currency": "USD"},
                123,
            )

    assert caught.value.status_code == 400
    assert caught.value.code == "42P10"
    assert "unique" in (caught.value.detail or "")


def test_telegram_schema_error_is_actionable() -> None:
    message = _database_error_message(
        SupabaseError(
            "schema mismatch",
            status_code=400,
            code="42P10",
        )
    )
    assert "supabase/supabase_migration_v1_3_2.sql" in message
    assert "42P10" in message


def test_telegram_permission_error_is_actionable() -> None:
    message = _database_error_message(
        SupabaseError("denied", status_code=403, code="42501")
    )
    assert "SUPABASE_SECRET_KEY" in message
    assert "service-role" in message


def test_schema_uses_non_partial_telegram_unique_index() -> None:
    from pathlib import Path

    schema = (
        Path(__file__).parents[1] / "supabase" / "supabase_schema.sql"
    ).read_text()
    index_section = schema.split("create unique index supporters_telegram_update_id_idx", 1)[1]
    index_section = index_section.split(";", 1)[0]
    assert "where telegram_update_id is not null" not in index_section.lower()


def test_schema_and_migration_include_visit_analytics() -> None:
    from pathlib import Path

    supabase = Path(__file__).parents[1] / "supabase"
    schema = (supabase / "supabase_schema.sql").read_text()
    migration = (
        supabase / "supabase_migration_v2_4_0_visit_analytics.sql"
    ).read_text()

    assert "analytics jsonb not null default '{}'::jsonb" in schema.lower()
    assert "add column if not exists analytics jsonb" in migration.lower()
    assert "notify pgrst, 'reload schema'" in migration.lower()
