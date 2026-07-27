from __future__ import annotations

import httpx
from starlette.requests import Request

from app.config import Settings
from app.models import VisitPayload
from app.services.supabase import SupabaseService
from app.services.telegram import TelegramService
from app.services.visits import VisitService


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/website/visit",
            "headers": [(b"user-agent", b"Test Agent")],
            "client": ("198.51.100.20", 1234),
            "server": ("test", 80),
            "scheme": "http",
            "query_string": b"",
        }
    )


async def test_visit_urls_drop_queries_fragments_and_client_timestamp() -> None:
    settings = Settings(
        require_encrypted_visits=False,
        trust_proxy_headers=False,
        visit_store_url_query=False,
    )
    async with httpx.AsyncClient() as client:
        service = VisitService(
            settings,
            SupabaseService(settings, client),
            TelegramService(settings, client),
        )
        payload = VisitPayload(
            eventId="client-controlled-id",
            timestamp="2000-01-01T00:00:00Z",
            url="https://example.com/pay?token=secret#section",
            referrer="https://search.example/?q=private",
            path="/wrong?secret=1",
        )
        public_visit, row, _, _ = service.build(_request(), payload)

    assert public_visit["url"] == "https://example.com/pay"
    assert public_visit["referrer"] == "https://search.example/"
    assert public_visit["path"] == "/pay"
    assert public_visit["event_id"] != "client-controlled-id"
    assert public_visit["timestamp"] != "2000-01-01T00:00:00Z"
    assert row["url"] == "https://example.com/pay"
