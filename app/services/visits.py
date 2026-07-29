from __future__ import annotations

import asyncio
import heapq
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from fastapi import Request

from app.config import Settings
from app.models import VisitPayload
from app.services.rate_limit import TokenBucketRateLimiter
from app.services.supabase import SupabaseError, SupabaseService
from app.services.telegram import TelegramResult, TelegramService
from app.utils.network import client_ip, proxy_is_trusted
from app.utils.security import mask_ip, random_id, sha256_text

_CAMPAIGN_QUERY_FIELDS = {
    "utm_source": "source",
    "utm_medium": "medium",
    "utm_campaign": "name",
    "utm_id": "campaignId",
    "utm_term": "term",
    "utm_content": "content",
}


def _sanitize_url(value: str, *, keep_query: bool) -> str:
    text = str(value or "").strip()[:1500]
    if not text or text == "Direct visit":
        return text
    try:
        parsed = urlsplit(text)
    except ValueError:
        return ""
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return ""

    host = parsed.hostname
    try:
        port = parsed.port
    except ValueError:
        return ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    if port is not None:
        host = f"{host}:{port}"
    return urlunsplit(
        (
            parsed.scheme.lower(),
            host,
            parsed.path or "/",
            parsed.query if keep_query else "",
            "",
        )
    )


def _clean_field(value: str, maximum: int) -> str:
    text = "".join(
        " " if character in "\r\n\t" else character
        for character in str(value or "")
        if ord(character) >= 32 and ord(character) != 127
    )
    return " ".join(text.split())[:maximum]


def _campaign_from_url(value: str) -> dict[str, str]:
    try:
        parsed = urlsplit(str(value or "")[:1500])
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            return {}
        query_items = parse_qsl(
            parsed.query,
            keep_blank_values=False,
            max_num_fields=50,
        )
    except ValueError:
        return {}

    campaign: dict[str, str] = {}
    for key, value in query_items:
        field = _CAMPAIGN_QUERY_FIELDS.get(key.lower())
        if field and field not in campaign:
            cleaned = _clean_field(value, 160)
            if cleaned:
                campaign[field] = cleaned
    return campaign


def _request_client_hints(request: Request) -> dict[str, str | bool]:
    hints: dict[str, str | bool] = {}
    header_fields = {
        "sec-ch-ua": ("brands", 300),
        "sec-ch-ua-platform": ("platform", 80),
        "sec-ch-ua-platform-version": ("platformVersion", 80),
        "accept-language": ("acceptLanguage", 160),
    }
    for header, (field, maximum) in header_fields.items():
        cleaned = _clean_field(request.headers.get(header, ""), maximum)
        if cleaned:
            hints[field] = cleaned

    mobile = request.headers.get("sec-ch-ua-mobile", "").strip()
    if mobile in {"?0", "?1"}:
        hints["mobile"] = mobile == "?1"
    return hints


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

    def _proxy_is_trusted(self, request: Request) -> bool:
        return proxy_is_trusted(request, self.settings)

    def _client_ip(self, request: Request) -> str:
        return client_ip(request, self.settings)

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
        now = datetime.now(UTC)
        server_timestamp = now.isoformat()
        ip = self._client_ip(request)
        user_agent = _clean_field(request.headers.get("user-agent", "Unknown"), 1000) or "Unknown"
        visit_url = _sanitize_url(
            payload.url,
            keep_query=self.settings.visit_store_url_query,
        )
        referrer = _sanitize_url(
            payload.referrer,
            keep_query=self.settings.visit_store_url_query,
        ) or "Direct visit"
        path = urlsplit(visit_url).path if visit_url else str(payload.path or "/").split("?", 1)[0]
        path = path[:500] if path.startswith("/") else "/"
        cooldown_seconds = self.settings.visit_alert_cooldown_minutes * 60
        bucket = int(now.timestamp()) // cooldown_seconds
        ip_hash = sha256_text(f"{self.settings.visit_hash_salt}:{ip}")
        local_dedupe_key = sha256_text(f"{ip_hash}:{user_agent}")
        database_dedupe_key = sha256_text(f"{local_dedupe_key}:{bucket}")
        location, country, region, city = self._location(request)
        screen = payload.screen.model_dump()
        connection = payload.connection.model_dump()
        connection["type"] = _clean_field(str(connection.get("type", "")), 40)
        connection["effectiveType"] = _clean_field(
            str(connection.get("effectiveType", "")),
            80,
        )
        viewport = f"{screen.get('viewportWidth', 0)}x{screen.get('viewportHeight', 0)}"

        analytics: dict[str, Any] = {}
        if self.settings.visit_detailed_analytics_enabled:
            payload_campaign: dict[str, str] = {}
            for key, value in payload.campaign.model_dump(exclude_none=True).items():
                cleaned = _clean_field(str(value), 160)
                if cleaned:
                    payload_campaign[key] = cleaned
            campaign = {
                **payload_campaign,
                **_campaign_from_url(payload.url),
            }

            navigation = payload.navigation.model_dump(exclude_none=True)
            navigation["type"] = _clean_field(
                str(navigation.get("type", "Unknown")),
                40,
            )
            capabilities = payload.capabilities.model_dump(exclude_none=True)

            raw_session_id = _clean_field(payload.session.id or "", 128)
            session: dict[str, Any] = {
                "pageViews": payload.session.pageViews,
                "returningVisitor": payload.session.returningVisitor,
            }
            if raw_session_id:
                session["id"] = sha256_text(
                    f"{self.settings.visit_hash_salt}:session:{raw_session_id}"
                )[:16]

            analytics = {
                "campaign": campaign,
                "navigation": navigation,
                "capabilities": capabilities,
                "session": session,
                "clientHints": _request_client_hints(request),
            }
            request_id = _clean_field(
                str(getattr(request.state, "request_id", "")),
                64,
            )
            if request_id:
                analytics["requestId"] = request_id

        public_visit = {
            "event_id": random_id(),
            "timestamp": server_timestamp,
            "local_time": _clean_field(payload.localTime or server_timestamp, 120),
            "url": visit_url,
            "path": path,
            "referrer": referrer,
            "title": _clean_field(payload.title, 300),
            "device": _clean_field(payload.device, 120),
            "browser": _clean_field(payload.browser, 160),
            "platform": _clean_field(payload.platform, 160),
            "language": _clean_field(payload.language, 40),
            "timezone": _clean_field(payload.timezone, 100),
            "screen": screen,
            "connection": connection,
            "analytics": analytics,
            "viewport": viewport,
            "location": location,
            "visitor_id": ip_hash[:12],
            "masked_ip": mask_ip(ip),
            "user_agent": user_agent,
        }
        row = {
            "event_id": public_visit["event_id"],
            "dedupe_key": database_dedupe_key,
            "client_timestamp": server_timestamp,
            "url": visit_url or None,
            "path": path,
            "referrer": referrer if referrer != "Direct visit" else None,
            "title": public_visit["title"] or None,
            "device": public_visit["device"],
            "browser": public_visit["browser"],
            "platform": public_visit["platform"],
            "language": public_visit["language"],
            "timezone": public_visit["timezone"],
            "screen": screen,
            "connection": connection,
            "analytics": analytics,
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
                    datetime.now(UTC) - timedelta(seconds=cooldown_seconds)
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
