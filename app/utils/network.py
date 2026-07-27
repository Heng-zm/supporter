from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from starlette.requests import Request
from starlette.types import Scope

from app.config import Settings
from app.utils.security import ip_is_trusted, parse_ip


def _scope_headers(scope: Scope) -> dict[bytes, bytes]:
    return {key.lower(): value for key, value in scope.get("headers", [])}


def peer_ip_from_scope(scope: Scope) -> str:
    client = scope.get("client")
    if not client:
        return "unknown"
    return str(client[0])


def peer_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def proxy_is_trusted_scope(scope: Scope, settings: Settings) -> bool:
    return bool(
        settings.trust_proxy_headers
        and ip_is_trusted(peer_ip_from_scope(scope), settings.trusted_proxy_networks)
    )


def proxy_is_trusted(request: Request, settings: Settings) -> bool:
    return bool(
        settings.trust_proxy_headers
        and ip_is_trusted(peer_ip(request), settings.trusted_proxy_networks)
    )


def _forwarded_chain(value: str) -> list[str]:
    return [
        address.compressed
        for item in value.split(",")
        if (address := parse_ip(item)) is not None
    ]


def resolve_client_ip(
    peer: str,
    forwarded_for: str,
    trusted_networks: Iterable[Any],
) -> str:
    chain = _forwarded_chain(forwarded_for)
    chain.append(peer)

    for candidate in reversed(chain):
        if not ip_is_trusted(candidate, trusted_networks):
            return candidate
    return chain[0] if chain else peer


def client_ip(request: Request, settings: Settings) -> str:
    direct_peer = peer_ip(request)
    if not proxy_is_trusted(request, settings):
        return direct_peer
    return resolve_client_ip(
        direct_peer,
        request.headers.get("x-forwarded-for", ""),
        settings.trusted_proxy_networks,
    )


def client_ip_from_scope(scope: Scope, settings: Settings) -> str:
    direct_peer = peer_ip_from_scope(scope)
    if not proxy_is_trusted_scope(scope, settings):
        return direct_peer
    headers = _scope_headers(scope)
    forwarded = headers.get(b"x-forwarded-for", b"").decode("latin-1", errors="ignore")
    return resolve_client_ip(direct_peer, forwarded, settings.trusted_proxy_networks)


def request_is_https_scope(scope: Scope, settings: Settings) -> bool:
    if str(scope.get("scheme", "")).lower() == "https":
        return True
    if not proxy_is_trusted_scope(scope, settings):
        return False
    headers = _scope_headers(scope)
    forwarded_proto = headers.get(b"x-forwarded-proto", b"")
    first_value = forwarded_proto.decode("latin-1", errors="ignore").split(",", 1)[0]
    return first_value.strip().lower() == "https"


def request_is_https(request: Request, settings: Settings) -> bool:
    return request_is_https_scope(request.scope, settings)
