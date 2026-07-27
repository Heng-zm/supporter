from __future__ import annotations

import asyncio
import heapq
import math
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import Request

from app.config import Settings
from app.models import VisitPayload
from app.services.supabase import SupabaseError, SupabaseService
from app.services.telegram import TelegramResult, TelegramService
from app.utils.security import ip_is_trusted, mask_ip, parse_ip, random_id, sha256_text


class VisitRateLimitError(RuntimeError):
    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__("Too many visit requests.")
        self.retry_after_seconds = max(1, retry_after_seconds)


class ExpiringReservationCache:
    def __init__(self, max_items: int = 5000) -> None:
        self.max_items = max_items
        self._items: dict[str, float] = {}
        self._heap: list[tuple[float, str]] = []
        self._lock = asyncio.Lock()

    def _remove_expired(self, now: float) -> None:
        while self._heap and self._heap[0][0] <= now:
            expires_at, key = heapq.heappop(self._heap)
            if self._items.get(key) == expires_at:
                self._items.pop(key, None)

    async def reserve(self, key: str, ttl_seconds: int) -> bool:
        now = time.monotonic()
        async with self._lock:
            self._remove_expired(now)
            if key in self._items:
                return False
            if len(self._items) >= self.max_items:
                while self._heap:
                    expires_at, oldest_key = heapq.heappop(self._heap)
                    if self._items.get(oldest_key) == expires_at:
                        self._items.pop(oldest_key, None)
                        break
            expires_at = now + ttl_seconds
            self._items[key] = expires_at
            heapq.heappush(self._heap, (expires_at, key))
            return True

    async def commit(self, key: str, ttl_seconds: int) -> None:
        now = time.monotonic()
        async with self._lock:
            self._remove_expired(now)
            expires_at = now + ttl_seconds
            self._items[key] = expires_at
            heapq.heappush(self._heap, (expires_at, key))

    async def release(self, key: str) -> None:
        async with self._lock:
            self._items.pop(key, None)


class TokenBucketRateLimiter:
    def __init__(self, max_items: int = 10000) -> None:
        self.max_items = max_items
        self._items: OrderedDict[str, tuple[float, float]] = OrderedDict()
        self._lock = asyncio.Lock()

    async def check(self, key: str, limit: int, window_seconds: int = 60) -> int | None:
        now = time.monotonic()
        refill_rate = limit / window_seconds

        async with self._lock:
            tokens, updated_at = self._items.get(key, (float(limit), now))
            tokens = min(float(limit), tokens + max(0.0, now - updated_at) * refill_rate)

            if tokens >= 1.0:
                tokens -= 1.0
                retry_after = None
            else:
                retry_after = max(1, math.ceil((1.0 - tokens) / refill_rate))

            self._items[key] = (tokens, now)
            self._items.move_to_end(key)
            while len(self._items) > self.max_items:
                self._items.popitem(last=False)

            return retry_after


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
        self.cache = ExpiringReservationCache()
        self.rate_limiter = TokenBucketRateLimiter()

    def _peer_ip(self, request: Request) -> str:
        return request.client.host if request.client else "unknown"

    def _proxy_is_trusted(self, request: Request) -> bool:
        return bool(
            self.settings.trust_proxy_headers
            and ip_is_trusted(self._peer_ip(request), self.settings.trusted_proxy_networks)
        )

    def _client_ip(self, request: Request) -> str:
        peer_ip = self._peer_ip(request)
        if not self._proxy_is_trusted(request):
            return peer_ip

        forwarded = request.headers.get("x-forwarded-for", "")
        chain = [
            address.compressed
            for item in forwarded.split(",")
            if (address := parse_ip(item)) is not None
        ]
        chain.append(peer_ip)

        for candidate in reversed(chain):
            if not ip_is_trusted(candidate, self.settings.trusted_proxy_networks):
                return candidate
        return chain[0] if chain else peer_ip

    def _location(self, request: Request) -> tuple[str, str | None, str | None, str | None]:
        if not self._proxy_is_trusted(request):
            return "Unknown", None, None, None
        country = request.headers.get("x-vercel-ip-country") or request.headers.get("cf-ipcountry")
        region = request.headers.get("x-vercel-ip-country-region")
        city = request.headers.get("x-vercel-ip-city")
        value = ", ".join(part for part in (city, region, country) if part) or "Unknown"
        return value, country, region, city

    async def enforce_rate_limit(self, request: Request) -> None:
        client_ip = self._client_ip(request)
        key = sha256_text(f"{self.settings.visit_hash_salt}:rate:{client_ip}")
        retry_after = await self.rate_limiter.check(
            key,
            self.settings.visit_rate_limit_per_minute,
        )
        if retry_after is not None:
            raise VisitRateLimitError(retry_after)

    def build(
        self,
        request: Request,
        payload: VisitPayload,
    ) -> tuple[dict[str, Any], dict[str, Any], str, str]:
        now = datetime.now(timezone.utc)
        ip = self._client_ip(request)
        user_agent = request.headers.get("user-agent", "Unknown").strip()[:1000] or "Unknown"
        cooldown_seconds = self.settings.visit_alert_cooldown_minutes * 60
        bucket = int(now.timestamp()) // cooldown_seconds
        ip_hash = sha256_text(f"{self.settings.visit_hash_salt}:{ip}")
        local_dedupe_key = sha256_text(f"{ip_hash}:{user_agent}")
        database_dedupe_key = sha256_text(f"{local_dedupe_key}:{bucket}")
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
            "dedupe_key": database_dedupe_key,
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
        return public_visit, row, local_dedupe_key, database_dedupe_key

    async def process(self, request: Request, payload: VisitPayload) -> VisitProcessResult:
        public_visit, row, local_key, database_key = self.build(request, payload)
        cooldown_seconds = self.settings.visit_alert_cooldown_minutes * 60
        in_flight_ttl = min(60, cooldown_seconds)

        if not await self.cache.reserve(local_key, in_flight_ttl):
            return VisitProcessResult(
                duplicate=True,
                stored=False,
                telegram=TelegramResult(ok=False, skipped=True, error="Duplicate visit."),
            )

        stored: dict[str, Any] | None = None
        storage_error: Exception | None = None

        try:
            if self.supabase.enabled:
                since_iso = (
                    datetime.now(timezone.utc) - timedelta(seconds=cooldown_seconds)
                ).isoformat()
                try:
                    stored = await self.supabase.find_recent_visit(
                        ip_hash=str(row["ip_hash"]),
                        user_agent=str(row["user_agent"]),
                        since_iso=since_iso,
                    )
                    if stored is not None:
                        already_sent = bool(stored.get("telegram_sent"))
                        alerts_disabled = not self.settings.telegram_visit_alert_enabled
                        if already_sent or alerts_disabled:
                            await self.cache.commit(local_key, cooldown_seconds)
                            return VisitProcessResult(
                                duplicate=True,
                                stored=True,
                                telegram=TelegramResult(
                                    ok=already_sent,
                                    skipped=not already_sent,
                                    message_id=(
                                        str(stored["telegram_message_id"])
                                        if stored.get("telegram_message_id") is not None
                                        else None
                                    ),
                                    error="Duplicate visit.",
                                ),
                            )
                    else:
                        stored = await self.supabase.insert_visit_once(row)
                        if stored is None:
                            stored = await self.supabase.get_visit_by_dedupe_key(database_key)
                        if stored is None and self.settings.require_visit_storage:
                            storage_error = RuntimeError(
                                "Supabase did not confirm visit storage."
                            )
                except SupabaseError as exc:
                    storage_error = exc
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
                except SupabaseError:
                    pass

            if self.settings.telegram_visit_alert_enabled:
                durable_result = telegram.ok
            else:
                durable_result = bool(stored) or telegram.skipped

            if durable_result:
                await self.cache.commit(local_key, cooldown_seconds)
            else:
                await self.cache.release(local_key)

            return VisitProcessResult(
                duplicate=False,
                stored=bool(stored),
                telegram=telegram,
            )
        except Exception:
            await self.cache.release(local_key)
            raise
