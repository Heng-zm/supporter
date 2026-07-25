from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from fastapi import Request

from app.config import Settings
from app.models import VisitPayload
from app.services.supabase import SupabaseService
from app.services.telegram import TelegramResult, TelegramService
from app.utils.security import mask_ip, random_id, sha256_text


class RecentVisitCache:
    def __init__(self, max_items: int = 5000) -> None:
        self.max_items = max_items
        self._items: dict[str, float] = {}
        self._lock = asyncio.Lock()

    async def reserve(self, key: str, ttl_seconds: int) -> bool:
        now = time.monotonic()
        async with self._lock:
            expired = [item for item, expires_at in self._items.items() if expires_at <= now]
            for item in expired:
                self._items.pop(item, None)
            if key in self._items:
                return False
            if len(self._items) >= self.max_items:
                oldest = min(self._items, key=self._items.get)
                self._items.pop(oldest, None)
            self._items[key] = now + ttl_seconds
            return True


@dataclass(slots=True)
class VisitProcessResult:
    duplicate: bool
    stored: bool
    telegram: TelegramResult


class VisitService:
    def __init__(
        self,
        settings: Settings,
        supabase: SupabaseService,
        telegram: TelegramService,
    ) -> None:
        self.settings = settings
        self.supabase = supabase
        self.telegram = telegram
        self.cache = RecentVisitCache()

    def _client_ip(self, request: Request) -> str:
        if self.settings.trust_proxy_headers:
            forwarded = request.headers.get("x-forwarded-for", "")
            if forwarded:
                return forwarded.split(",", 1)[0].strip()
            real_ip = request.headers.get("x-real-ip", "").strip()
            if real_ip:
                return real_ip
        return request.client.host if request.client else "unknown"

    @staticmethod
    def _location(request: Request) -> tuple[str, str | None, str | None, str | None]:
        country = request.headers.get("x-vercel-ip-country") or request.headers.get("cf-ipcountry")
        region = request.headers.get("x-vercel-ip-country-region")
        city = request.headers.get("x-vercel-ip-city")
        value = ", ".join(part for part in (city, region, country) if part) or "Unknown"
        return value, country, region, city

    def build(self, request: Request, payload: VisitPayload) -> tuple[dict[str, Any], dict[str, Any], str]:
        now = datetime.now(timezone.utc)
        ip = self._client_ip(request)
        user_agent = payload.userAgent or request.headers.get("user-agent", "Unknown")
        cooldown_seconds = self.settings.visit_alert_cooldown_minutes * 60
        bucket = int(now.timestamp()) // cooldown_seconds
        ip_hash = sha256_text(f"{self.settings.visit_hash_salt}:{ip}")
        dedupe_key = sha256_text(f"{ip_hash}:{user_agent}:{bucket}")
        location, country, region, city = self._location(request)
        screen = payload.screen.model_dump()
        connection = payload.connection.model_dump()
        viewport = f"{screen.get('viewportWidth', 0)}x{screen.get('viewportHeight', 0)}"

        public_visit = {
            "event_id": payload.eventId or random_id(),
            "timestamp": payload.timestamp or now.isoformat(),
            "local_time": payload.localTime or now.isoformat(),
            "url": payload.url,
            "path": payload.path,
            "referrer": payload.referrer,
            "title": payload.title,
            "device": payload.device,
            "browser": payload.browser,
            "platform": payload.platform,
            "language": payload.language,
            "timezone": payload.timezone,
            "screen": screen,
            "connection": connection,
            "viewport": viewport,
            "location": location,
            "visitor_id": ip_hash[:12],
            "masked_ip": mask_ip(ip),
            "user_agent": user_agent,
        }
        row = {
            "event_id": public_visit["event_id"],
            "dedupe_key": dedupe_key,
            "client_timestamp": public_visit["timestamp"],
            "url": payload.url or None,
            "path": payload.path,
            "referrer": payload.referrer or None,
            "title": payload.title or None,
            "device": payload.device,
            "browser": payload.browser,
            "platform": payload.platform,
            "language": payload.language,
            "timezone": payload.timezone,
            "screen": screen,
            "connection": connection,
            "user_agent": user_agent,
            "ip_hash": ip_hash,
            "ip_masked": public_visit["masked_ip"],
            "country": country,
            "region": region,
            "city": city,
        }
        return public_visit, row, dedupe_key

    async def process(self, request: Request, payload: VisitPayload) -> VisitProcessResult:
        public_visit, row, dedupe_key = self.build(request, payload)
        ttl = self.settings.visit_alert_cooldown_minutes * 60
        if not await self.cache.reserve(dedupe_key, ttl):
            return VisitProcessResult(
                duplicate=True,
                stored=False,
                telegram=TelegramResult(ok=False, skipped=True, error="Duplicate visit."),
            )

        stored: dict[str, Any] | None = None
        storage_error: Exception | None = None
        if self.supabase.enabled:
            try:
                stored = await self.supabase.insert_visit_once(row)
            except Exception as exc:  # External provider error is handled below.
                storage_error = exc
            if stored is None and storage_error is None:
                return VisitProcessResult(
                    duplicate=True,
                    stored=False,
                    telegram=TelegramResult(ok=False, skipped=True, error="Duplicate visit."),
                )
        elif self.settings.require_visit_storage:
            storage_error = RuntimeError("Supabase is not configured.")

        if storage_error and self.settings.require_visit_storage:
            raise RuntimeError("Visit storage is unavailable.") from storage_error

        telegram = await self.telegram.send_visit(public_visit)
        if stored and stored.get("id"):
            try:
                await self.supabase.update_visit_delivery(
                    str(stored["id"]),
                    sent=telegram.ok,
                    message_id=telegram.message_id,
                    error=telegram.error,
                )
            except Exception:
                pass

        return VisitProcessResult(
            duplicate=False,
            stored=bool(stored),
            telegram=telegram,
        )
