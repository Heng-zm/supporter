from __future__ import annotations

import httpx
from starlette.requests import Request

from app.config import Settings
from app.models import VisitPayload
from app.services.supabase import SupabaseService
from app.services.telegram import TelegramService
from app.services.visits import VisitService


def _request(*, detailed: bool = False) -> Request:
    headers = [(b"user-agent", b"Test Agent")]
    state: dict[str, str] = {}
    if detailed:
        headers.extend(
            [
                (b"sec-ch-ua", b'"Chromium";v="126", "Not/A)Brand";v="8"'),
                (b"sec-ch-ua-mobile", b"?1"),
                (b"sec-ch-ua-platform", b'"Android"'),
                (b"accept-language", b"en-US,en;q=0.9"),
            ]
        )
        state["request_id"] = "request-id-analytics-123"

    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/website/visit",
            "headers": headers,
            "client": ("198.51.100.20", 1234),
            "server": ("test", 80),
            "scheme": "http",
            "query_string": b"",
            "state": state,
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


async def test_visit_urls_reject_embedded_control_characters() -> None:
    settings = Settings(
        require_encrypted_visits=False,
        trust_proxy_headers=False,
    )
    async with httpx.AsyncClient() as client:
        service = VisitService(
            settings,
            SupabaseService(settings, client),
            TelegramService(settings, client),
        )
        public_visit, row, _, _ = service.build(
            _request(),
            VisitPayload(
                url="https://example.com/pay\nforged",
                referrer="https://referrer.example/\tforged",
            ),
        )

    assert public_visit["url"] == ""
    assert public_visit["referrer"] == "Direct visit"
    assert row["url"] is None
    assert row["referrer"] is None


async def test_detailed_analytics_are_bounded_hashed_and_alert_ready() -> None:
    settings = Settings(
        require_encrypted_visits=False,
        trust_proxy_headers=False,
        visit_store_url_query=False,
        visit_detailed_analytics_enabled=True,
    )
    async with httpx.AsyncClient() as client:
        telegram = TelegramService(settings, client)
        service = VisitService(
            settings,
            SupabaseService(settings, client),
            telegram,
        )
        payload = VisitPayload.model_validate(
            {
                "url": (
                    "https://example.com/pay?utm_source=newsletter"
                    "&utm_medium=email&utm_campaign=summer&token=secret"
                ),
                "title": "Donation checkout",
                "connection": {
                    "online": True,
                    "type": "wifi",
                    "effectiveType": "4g",
                    "downlinkMbps": 25.5,
                    "rttMs": 42,
                    "saveData": False,
                },
                "navigation": {
                    "type": "navigate",
                    "durationMs": 900.5,
                    "domContentLoadedMs": 480,
                    "loadTimeMs": 760,
                    "transferSizeBytes": 120000,
                },
                "capabilities": {
                    "memoryGb": 8,
                    "logicalProcessors": 8,
                    "maxTouchPoints": 5,
                    "colorDepth": 24,
                    "cookiesEnabled": True,
                    "doNotTrack": True,
                },
                "session": {
                    "id": "raw-session-id-must-not-be-stored",
                    "pageViews": 4,
                    "returningVisitor": True,
                },
            }
        )
        public_visit, row, _, _ = service.build(
            _request(detailed=True),
            payload,
        )
        message = telegram.build_visit_message(public_visit)

    analytics = row["analytics"]
    assert analytics["campaign"] == {
        "source": "newsletter",
        "medium": "email",
        "name": "summer",
    }
    assert analytics["navigation"]["loadTimeMs"] == 760
    assert analytics["capabilities"]["logicalProcessors"] == 8
    assert analytics["session"]["pageViews"] == 4
    assert analytics["session"]["returningVisitor"] is True
    assert len(analytics["session"]["id"]) == 16
    assert analytics["session"]["id"] != "raw-session-id-must-not-be-stored"
    assert analytics["clientHints"]["mobile"] is True
    assert analytics["requestId"] == "request-id-analytics-123"
    assert "token" not in str(analytics)
    assert "secret" not in str(analytics)
    assert row["url"] == "https://example.com/pay"

    assert "Campaign:</b> newsletter / email / summer" in message
    assert "Performance:</b> navigate" in message
    assert "Capabilities:</b> 8 GB RAM" in message
    assert "Session:</b>" in message
    assert "request-id-analytics-123" in message
    assert "raw-session-id-must-not-be-stored" not in message


async def test_detailed_analytics_can_be_disabled() -> None:
    settings = Settings(
        require_encrypted_visits=False,
        trust_proxy_headers=False,
        visit_detailed_analytics_enabled=False,
    )
    async with httpx.AsyncClient() as client:
        service = VisitService(
            settings,
            SupabaseService(settings, client),
            TelegramService(settings, client),
        )
        payload = VisitPayload.model_validate(
            {
                "url": "https://example.com/?utm_source=private-campaign",
                "session": {"id": "must-not-be-processed"},
                "capabilities": {"memoryGb": 16},
            }
        )
        public_visit, row, _, _ = service.build(_request(detailed=True), payload)

    assert public_visit["analytics"] == {}
    assert row["analytics"] == {}
