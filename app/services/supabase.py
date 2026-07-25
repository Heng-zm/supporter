from __future__ import annotations

import asyncio
import time
from typing import Any
from urllib.parse import quote

import httpx

from app.config import Settings


PUBLIC_SUPPORTER_COLUMNS = (
    "id,name,amount,currency,message,avatar_url,payment_method,created_at"
)


class SupabaseError(RuntimeError):
    pass


class SupabaseService:
    def __init__(self, settings: Settings, client: httpx.AsyncClient) -> None:
        self.settings = settings
        self.client = client
        self.base_url = settings.supabase_url.rstrip("/")
        self.secret_key = settings.supabase_secret_key.strip()
        self._supporters_cache: list[dict[str, Any]] = []
        self._supporters_cache_at = 0.0
        self._supporters_cache_limit = 0
        self._supporters_lock = asyncio.Lock()

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

    async def _request(
        self,
        method: str,
        table: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | list[dict[str, Any]] | None = None,
        prefer: str | None = None,
    ) -> Any:
        if not self.enabled:
            raise SupabaseError("Supabase is not configured.")

        try:
            response = await self.client.request(
                method,
                f"{self.base_url}/rest/v1/{table}",
                params=params,
                json=body,
                headers=self._headers(prefer),
            )
        except httpx.HTTPError as exc:
            raise SupabaseError("Unable to reach Supabase.") from exc

        if response.status_code >= 400:
            detail = response.text[:1000]
            raise SupabaseError(
                f"Supabase returned {response.status_code}: {detail}"
            )
        if response.status_code == 204 or not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise SupabaseError("Supabase returned invalid JSON.") from exc

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

    def invalidate_supporters_cache(self) -> None:
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

    async def list_supporters(self, limit: int) -> list[dict[str, Any]]:
        """Read current data from Supabase and refresh the in-memory cache."""
        rows = await self._fetch_supporters(limit)
        self._write_supporters_cache(rows, limit)
        return rows

    async def list_supporters_resilient(
        self,
        limit: int,
    ) -> tuple[list[dict[str, Any]], str, bool]:
        """Return fresh supporters, using a short cache and stale fallback.

        The browser already has its own local cache. This server cache reduces
        Supabase requests and keeps the public list available during short
        provider/network failures without exposing an error panel in the UI.
        """
        fresh = self._read_supporters_cache(
            limit,
            max_age_seconds=self.settings.supporters_cache_ttl_seconds,
        )
        if fresh is not None:
            return fresh, "memory-cache", False

        async with self._supporters_lock:
            fresh = self._read_supporters_cache(
                limit,
                max_age_seconds=self.settings.supporters_cache_ttl_seconds,
            )
            if fresh is not None:
                return fresh, "memory-cache", False

            try:
                rows = await self._fetch_supporters(limit)
            except SupabaseError:
                stale = self._read_supporters_cache(
                    limit,
                    max_age_seconds=self.settings.supporters_stale_cache_seconds,
                )
                if stale is not None:
                    return stale, "stale-memory-cache", True
                raise

            self._write_supporters_cache(rows, limit)
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
        self.invalidate_supporters_cache()
        return rows[0]

    async def update_supporter(
        self,
        supporter_id: str,
        patch: dict[str, Any],
    ) -> dict[str, Any] | None:
        rows = await self._request(
            "PATCH",
            "supporters",
            params={"id": f"eq.{supporter_id}", "select": PUBLIC_SUPPORTER_COLUMNS},
            body=patch,
            prefer="return=representation",
        )
        self.invalidate_supporters_cache()
        return rows[0] if isinstance(rows, list) and rows else None

    async def delete_supporter(self, supporter_id: str) -> None:
        await self._request(
            "DELETE",
            "supporters",
            params={"id": f"eq.{supporter_id}"},
            prefer="return=minimal",
        )
        self.invalidate_supporters_cache()

    async def insert_visit_once(self, row: dict[str, Any]) -> dict[str, Any] | None:
        rows = await self._request(
            "POST",
            "visit_events",
            params={"on_conflict": "dedupe_key", "select": "id"},
            body=row,
            prefer="resolution=ignore-duplicates,return=representation",
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
        )
