from __future__ import annotations

from fastapi import Header, HTTPException, Request, status

from app.config import Settings
from app.services.supabase import SupabaseService
from app.services.telegram import TelegramService
from app.services.telegram_commands import TelegramCommandService
from app.services.visit_crypto import VisitCryptoService
from app.services.visits import VisitService
from app.utils.network import client_ip
from app.utils.security import ip_is_trusted, secure_equals, sha256_text


def get_app_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_supabase(request: Request) -> SupabaseService:
    return request.app.state.supabase


def get_telegram(request: Request) -> TelegramService:
    return request.app.state.telegram


def get_telegram_commands(request: Request) -> TelegramCommandService:
    return request.app.state.telegram_commands


def get_visit_service(request: Request) -> VisitService:
    return request.app.state.visits


def get_visit_crypto(request: Request) -> VisitCryptoService:
    return request.app.state.visit_crypto


def require_json_content_type(request: Request) -> None:
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "application/json":
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="Content-Type must be application/json.",
            headers={"Cache-Control": "no-store"},
        )


async def require_admin(
    request: Request,
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key"),
) -> None:
    settings: Settings = request.app.state.settings
    if not settings.supporters_admin_api_enabled:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Not found.",
            headers={"Cache-Control": "no-store"},
        )

    resolved_ip = client_ip(request, settings)
    allowed_networks = settings.admin_allowed_networks
    if allowed_networks and not ip_is_trusted(resolved_ip, allowed_networks):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Not found.",
            headers={"Cache-Control": "no-store"},
        )

    rate_key = sha256_text(f"{settings.visit_hash_salt}:admin:{resolved_ip}")
    retry_after = await request.app.state.admin_rate_limiter.check(
        rate_key,
        settings.admin_rate_limit_per_minute,
    )
    if retry_after is not None:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many admin requests.",
            headers={
                "Cache-Control": "no-store",
                "Retry-After": str(retry_after),
            },
        )

    supplied_key = x_admin_key or ""
    if len(supplied_key) > 512 or not secure_equals(supplied_key, settings.supporters_admin_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized.",
            headers={
                "Cache-Control": "no-store",
                "WWW-Authenticate": "ApiKey",
            },
        )
