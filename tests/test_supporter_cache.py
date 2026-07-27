from __future__ import annotations

import asyncio

import httpx

from app.config import Settings
from app.services.supabase import SupabaseService


class ConcurrentSupporterService(SupabaseService):
    def __init__(self, settings: Settings, client: httpx.AsyncClient) -> None:
        super().__init__(settings, client)
        self.fetch_started = asyncio.Event()
        self.release_first_fetch = asyncio.Event()
        self.fetch_calls = 0

    async def _fetch_supporters(self, limit: int) -> list[dict[str, object]]:
        self.fetch_calls += 1
        if self.fetch_calls == 1:
            self.fetch_started.set()
            await self.release_first_fetch.wait()
            return [{"id": "old", "name": "Old", "amount": 1, "currency": "USD"}]
        return [{"id": "new", "name": "New", "amount": 2, "currency": "USD"}]

    async def _request(self, method: str, table: str, **_: object) -> object:
        assert method == "POST"
        assert table == "supporters"
        return [{"id": "new", "name": "New", "amount": 2, "currency": "USD"}]


async def test_supporter_mutation_does_not_wait_for_slow_list_fetch() -> None:
    settings = Settings(
        supabase_url="https://example.supabase.co",
        supabase_secret_key="secret",
        require_encrypted_visits=False,
    )
    async with httpx.AsyncClient() as client:
        service = ConcurrentSupporterService(settings, client)
        list_task = asyncio.create_task(service.list_supporters(100))
        await service.fetch_started.wait()

        created = await asyncio.wait_for(
            service.create_supporter({"name": "New", "amount": 2, "currency": "USD"}),
            timeout=0.2,
        )
        assert created["id"] == "new"

        service.release_first_fetch.set()
        rows = await list_task

    assert rows[0]["id"] == "new"
    assert service.fetch_calls == 2
