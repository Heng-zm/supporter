from __future__ import annotations

import asyncio
import logging
import time
from typing import Any
from urllib.parse import quote

import httpx

from app.config import Settings


PUBLIC_SUPPORTER_COLUMNS = (
    "id,name,amount,currency,message,avatar_url,payment_method,created_at"
)
TRANSIENT_STATUS_CODES = {429, 502, 503, 504}
logger = logging.getLogger("app.supabase")


class SupabaseError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        code: str | None = None,
        detail: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.detail = detail

    @property
    def is_transient(self) -> bool:
        return self.status_code in TRANSIENT_STATUS_CODES or self.status_code is None


class SupabaseService:
    def __init__(self, settings: Settings, client: httpx.AsyncClient) -> None:
        self.settings = settings
        self.client = client
        self.base_url = settings.supabase_url.rstrip("/")
        self.secret_key = settings.supabase_secret_key.strip()
        self._supporters_cache: list[dict[str, Any]] = []
        self._supporters_cache_at = 0.0
        self._supporters_cache_limit = 0
        self._supporters_generation = 0
        self._supporters_cache_lock = asyncio.Lock()
        self._supporters_fetch_lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self.base_url and self.secret_key)

    def _headers(self, prefer: str | None = None) -> dict[str, str]:
        headers = {
            "apikey": self.secret_key,
            "Authorization": f"Bearer {self.secret_key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if prefer:
            headers["Prefer"] = prefer
        return headers

    @staticmethod
    def _retry_delay(response: httpx.Response, attempt: int) -> float:
        retry_after = response.headers.get("retry-after", "").strip()
        try:
            return min(5.0, max(0.0, float(retry_after)))
        except ValueError:
            return 0.15 * (2**attempt)

    async def _request(
        self,
        method: str,
        table: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | list[dict[str, Any]] | None = None,
        prefer: str | None = None,
        retry_safe: bool = False,
    ) -> Any:
        if not self.enabled:
            raise SupabaseError("Supabase is not configured.")

        attempts = 3 if method.upper() == "GET" or retry_safe else 1
        last_network_error: httpx.HTTPError | None = None

        for attempt in range(attempts):
            try:
                response = await self.client.request(
                    method,
                    f"{self.base_url}/rest/v1/{table}",
                    params=params,
                    json=body,
                    headers=self._headers(prefer),
                )
            except httpx.HTTPError as exc:
                last_network_error = exc
                if attempt + 1 < attempts:
                    await asyncio.sleep(0.15 * (2**attempt))
                    continue
                raise SupabaseError("Unable to reach Supabase.") from exc

            if response.status_code in TRANSIENT_STATUS_CODES and attempt + 1 < attempts:
                await asyncio.sleep(self._retry_delay(response, attempt))
                continue

            if response.status_code >= 400:
                raw_detail = response.text[:1000]
                error_code: str | None = None
                error_detail = raw_detail or "Unknown Supabase error."
                try:
                    payload = response.json()
                except ValueError:
                    payload = None
                if isinstance(payload, dict):
                    value = payload.get("code")
                    if value is not None:
                        error_code = str(value)
                    candidates = (
                        payload.get("message"),
                        payload.get("details"),
                        payload.get("hint"),
                    )
                    error_detail = next(
                        (str(item) for item in candidates if item),
                        error_detail,
                    )

                logger.warning(
                    "Supabase request failed: method=%s table=%s status=%s code=%s detail=%s",
                    method.upper(),
                    table,
                    response.status_code,
                    error_code or "unknown",
                    error_detail[:500],
                )
                raise SupabaseError(
                    f"Supabase returned {response.status_code}: {error_detail}",
                    status_code=response.status_code,
                    code=error_code,
                    detail=error_detail,
                )
            if response.status_code == 204 or not response.content:
                return None
            try:
                return response.json()
            except ValueError as exc:
                raise SupabaseError("Supabase returned invalid JSON.") from exc

        raise SupabaseError("Unable to reach Supabase.") from last_network_error

    def _supporters_cache_age(self) -> float:
        if not self._supporters_cache_at:
            return float("inf")
        return max(0.0, time.monotonic() - self._supporters_cache_at)

    def _read_supporters_cache(
        self,
        limit: int,
        *,
        max_age_seconds: int,
    ) -> list[dict[str, Any]] | None:
        if not self._supporters_cache_at:
            return None
        if self._supporters_cache_age() > max_age_seconds:
            return None
        if self._supporters_cache_limit < limit:
            return None
        return [dict(row) for row in self._supporters_cache[:limit]]

    def _write_supporters_cache(
        self,
        rows: list[dict[str, Any]],
        limit: int,
    ) -> None:
        self._supporters_cache = [dict(row) for row in rows]
        self._supporters_cache_at = time.monotonic()
        self._supporters_cache_limit = limit

    def _invalidate_supporters_cache_unlocked(self) -> None:
        self._supporters_cache = []
        self._supporters_cache_at = 0.0
        self._supporters_cache_limit = 0

    async def _fetch_supporters(self, limit: int) -> list[dict[str, Any]]:
        rows = await self._request(
            "GET",
            "supporters",
            params={
                "select": PUBLIC_SUPPORTER_COLUMNS,
                "is_visible": "eq.true",
                "order": "amount.desc,created_at.desc",
                "limit": str(limit),
            },
        )
        return rows if isinstance(rows, list) else []

    async def _cache_snapshot(
        self,
        limit: int,
        *,
        max_age_seconds: int,
    ) -> tuple[list[dict[str, Any]] | None, int]:
        async with self._supporters_cache_lock:
            return (
                self._read_supporters_cache(limit, max_age_seconds=max_age_seconds),
                self._supporters_generation,
            )

    async def _write_cache_if_current(
        self,
        rows: list[dict[str, Any]],
        limit: int,
        generation: int,
    ) -> bool:
        async with self._supporters_cache_lock:
            if generation != self._supporters_generation:
                return False
            self._write_supporters_cache(rows, limit)
            return True

    async def _invalidate_supporters_cache(self) -> None:
        async with self._supporters_cache_lock:
            self._supporters_generation += 1
            self._invalidate_supporters_cache_unlocked()

    async def _fetch_supporters_consistent(self, limit: int) -> list[dict[str, Any]]:
        # A mutation is allowed to run while Supabase is serving a list query.
        # If it completes during the fetch, retry once rather than caching an
        # older snapshot after the mutation.
        rows: list[dict[str, Any]] = []
        for _ in range(2):
            async with self._supporters_cache_lock:
                generation = self._supporters_generation
            rows = await self._fetch_supporters(limit)
            if await self._write_cache_if_current(rows, limit, generation):
                return rows
        return rows

    async def list_supporters(self, limit: int) -> list[dict[str, Any]]:
        async with self._supporters_fetch_lock:
            return await self._fetch_supporters_consistent(limit)

    async def list_supporters_resilient(
        self,
        limit: int,
    ) -> tuple[list[dict[str, Any]], str, bool]:
        fresh, _ = await self._cache_snapshot(
            limit,
            max_age_seconds=self.settings.supporters_cache_ttl_seconds,
        )
        if fresh is not None:
            return fresh, "memory-cache", False

        async with self._supporters_fetch_lock:
            fresh, _ = await self._cache_snapshot(
                limit,
                max_age_seconds=self.settings.supporters_cache_ttl_seconds,
            )
            if fresh is not None:
                return fresh, "memory-cache", False

            stale, stale_generation = await self._cache_snapshot(
                limit,
                max_age_seconds=self.settings.supporters_stale_cache_seconds,
            )
            try:
                rows = await self._fetch_supporters_consistent(limit)
            except SupabaseError:
                async with self._supporters_cache_lock:
                    generation_unchanged = (
                        stale_generation == self._supporters_generation
                    )
                if stale is not None and generation_unchanged:
                    return stale, "stale-memory-cache", True
                raise

            return rows, "supabase", False

    async def create_supporter(self, row: dict[str, Any]) -> dict[str, Any]:
        rows = await self._request(
            "POST",
            "supporters",
            params={"select": PUBLIC_SUPPORTER_COLUMNS},
            body=row,
            prefer="return=representation",
        )
        if not isinstance(rows, list) or not rows:
            raise SupabaseError("Supabase did not return the created supporter.")
        await self._invalidate_supporters_cache()
        return rows[0]

    async def create_supporter_from_telegram(
        self,
        row: dict[str, Any],
        update_id: int,
    ) -> dict[str, Any]:
        telegram_row = dict(row)
        telegram_row["telegram_update_id"] = update_id
        rows = await self._request(
            "POST",
            "supporters",
            params={
                "on_conflict": "telegram_update_id",
                "select": PUBLIC_SUPPORTER_COLUMNS,
            },
            body=telegram_row,
            prefer="resolution=ignore-duplicates,return=representation",
            retry_safe=True,
        )
        if not isinstance(rows, list) or not rows:
            rows = await self._request(
                "GET",
                "supporters",
                params={
                    "select": PUBLIC_SUPPORTER_COLUMNS,
                    "telegram_update_id": f"eq.{update_id}",
                    "limit": "1",
                },
            )
        if not isinstance(rows, list) or not rows:
            raise SupabaseError(
                "Supabase did not return the Telegram-created supporter."
            )
        await self._invalidate_supporters_cache()
        return rows[0]

    async def update_supporter(
        self,
        supporter_id: str,
        patch: dict[str, Any],
    ) -> dict[str, Any] | None:
        rows = await self._request(
            "PATCH",
            "supporters",
            params={
                "id": f"eq.{quote(supporter_id, safe='')}",
                "select": PUBLIC_SUPPORTER_COLUMNS,
            },
            body=patch,
            prefer="return=representation",
        )
        if isinstance(rows, list) and rows:
            await self._invalidate_supporters_cache()
            return rows[0]
        return None

    async def delete_supporter(self, supporter_id: str) -> bool:
        rows = await self._request(
            "DELETE",
            "supporters",
            params={
                "id": f"eq.{quote(supporter_id, safe='')}",
                "select": "id",
            },
            prefer="return=representation",
        )
        deleted = bool(isinstance(rows, list) and rows)
        if deleted:
            await self._invalidate_supporters_cache()
        return deleted

    async def find_recent_visit(
        self,
        *,
        ip_hash: str,
        user_agent: str,
        since_iso: str,
    ) -> dict[str, Any] | None:
        rows = await self._request(
            "GET",
            "visit_events",
            params={
                "select": "id,telegram_sent,telegram_message_id,telegram_error,created_at",
                "ip_hash": f"eq.{ip_hash}",
                "user_agent": f"eq.{user_agent}",
                "created_at": f"gte.{since_iso}",
                "order": "created_at.desc",
                "limit": "1",
            },
        )
        return rows[0] if isinstance(rows, list) and rows else None

    async def insert_visit_once(self, row: dict[str, Any]) -> dict[str, Any] | None:
        rows = await self._request(
            "POST",
            "visit_events",
            params={
                "on_conflict": "dedupe_key",
                "select": "id,telegram_sent,telegram_message_id,telegram_error,created_at",
            },
            body=row,
            prefer="resolution=ignore-duplicates,return=representation",
            retry_safe=True,
        )
        return rows[0] if isinstance(rows, list) and rows else None

    async def get_visit_by_dedupe_key(self, dedupe_key: str) -> dict[str, Any] | None:
        rows = await self._request(
            "GET",
            "visit_events",
            params={
                "select": "id,telegram_sent,telegram_message_id,telegram_error,created_at",
                "dedupe_key": f"eq.{dedupe_key}",
                "limit": "1",
            },
        )
        return rows[0] if isinstance(rows, list) and rows else None

    async def update_visit_delivery(
        self,
        visit_id: str,
        *,
        sent: bool,
        message_id: str | None,
        error: str | None,
    ) -> None:
        await self._request(
            "PATCH",
            "visit_events",
            params={"id": f"eq.{quote(visit_id, safe='')}"},
            body={
                "telegram_sent": sent,
                "telegram_message_id": message_id,
                "telegram_error": error[:500] if error else None,
            },
            prefer="return=minimal",
            retry_safe=True,
        )
