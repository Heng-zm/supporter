from __future__ import annotations

from fastapi import Header, HTTPException, Request, status

from app.config import Settings
from app.services.supabase import SupabaseService
from app.services.telegram import TelegramService
from app.services.visits import VisitService
from app.utils.security import secure_equals


def get_app_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_supabase(request: Request) -> SupabaseService:
    return request.app.state.supabase


def get_telegram(request: Request) -> TelegramService:
    return request.app.state.telegram


def get_visit_service(request: Request) -> VisitService:
    return request.app.state.visits


def require_admin(
    request: Request,
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key"),
) -> None:
    settings: Settings = request.app.state.settings
    if not settings.supporters_admin_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Supporter administration is not configured.",
        )
    if not secure_equals(x_admin_key or "", settings.supporters_admin_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized.",
        )
