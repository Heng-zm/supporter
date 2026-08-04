from __future__ import annotations

import httpx
from starlette.requests import Request

from app.config import Settings
from app.services.supabase import SupabaseService
from app.services.telegram import TelegramService
from app.services.visits import VisitService


def _request(peer: str, forwarded: str | None = None) -> Request:
    headers: list[tuple[bytes, bytes]] = []
    if forwarded is not None:
        headers.append((b"x-forwarded-for", forwarded.encode()))
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/",
        "headers": headers,
        "client": (peer, 1234),
        "server": ("test", 80),
        "scheme": "http",
        "query_string": b"",
    }
    return Request(scope)


async def test_uses_rightmost_untrusted_forwarded_address() -> None:
    settings = Settings(
        trust_proxy_headers=True,
        trusted_proxy_networks_raw="10.0.0.0/8",
        require_encrypted_visits=False,
    )
    async with httpx.AsyncClient() as client:
        service = VisitService(
            settings,
            SupabaseService(settings, client),
            TelegramService(settings, client),
        )
        request = _request("10.0.0.5", "198.51.100.4, 203.0.113.9")
        assert service._client_ip(request) == "203.0.113.9"


async def test_ignores_spoofed_header_from_untrusted_peer() -> None:
    settings = Settings(
        trust_proxy_headers=True,
        trusted_proxy_networks_raw="10.0.0.0/8",
        require_encrypted_visits=False,
    )
    async with httpx.AsyncClient() as client:
        service = VisitService(
            settings,
            SupabaseService(settings, client),
            TelegramService(settings, client),
        )
        request = _request("198.51.100.20", "203.0.113.9")
        assert service._client_ip(request) == "198.51.100.20"


def test_parsed_network_settings_are_cached() -> None:
    settings = Settings(
        require_encrypted_visits=False,
        trusted_proxy_networks_raw="10.0.0.0/8,2001:db8::/32",
        admin_allowed_networks_raw="192.0.2.0/24",
        telegram_webhook_allowed_networks_raw="198.51.100.0/24",
        telegram_admin_user_ids_raw="123,456",
    )

    assert settings.trusted_proxy_networks is settings.trusted_proxy_networks
    assert settings.admin_allowed_networks is settings.admin_allowed_networks
    assert (
        settings.telegram_webhook_allowed_networks
        is settings.telegram_webhook_allowed_networks
    )
    assert settings.telegram_admin_user_ids is settings.telegram_admin_user_ids
