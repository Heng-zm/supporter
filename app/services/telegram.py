from __future__ import annotations

import asyncio
from dataclasses import dataclass
from html import escape
from typing import Any

import httpx

from app.config import Settings


TRANSIENT_TELEGRAM_STATUS_CODES = {429, 500, 502, 503, 504}


@dataclass(slots=True)
class TelegramResult:
    ok: bool
    skipped: bool = False
    message_id: str | None = None
    error: str | None = None
    data: dict[str, Any] | None = None


class TelegramService:
    def __init__(self, settings: Settings, client: httpx.AsyncClient) -> None:
        self.settings = settings
        self.client = client

    @staticmethod
    def _text(value: Any, fallback: str = "Unknown", maximum: int = 900) -> str:
        text = str(value if value is not None else "").strip() or fallback
        return escape(text[:maximum], quote=True)

    @staticmethod
    def _retry_delay(response: httpx.Response, attempt: int) -> float:
        retry_after = response.headers.get("retry-after", "").strip()
        try:
            return min(5.0, max(0.0, float(retry_after)))
        except ValueError:
            return 0.2 * (2**attempt)

    async def _call(
        self,
        method: str,
        payload: dict[str, Any],
        *,
        retry_safe: bool = False,
    ) -> TelegramResult:
        if not self.settings.telegram_bot_enabled:
            return TelegramResult(
                ok=False,
                skipped=True,
                error="Telegram bot is not configured.",
            )

        url = f"https://api.telegram.org/bot{self.settings.telegram_bot_token}/{method}"
        attempts = 3 if retry_safe else 1

        for attempt in range(attempts):
            try:
                response = await self.client.post(url, json=payload)
            except httpx.HTTPError:
                if attempt + 1 < attempts:
                    await asyncio.sleep(0.2 * (2**attempt))
                    continue
                return TelegramResult(ok=False, error="Unable to reach Telegram.")

            try:
                decoded = response.json() if response.content else {}
            except ValueError:
                decoded = {}
            data = decoded if isinstance(decoded, dict) else {}

            error_code = data.get("error_code")
            transient_code = (
                error_code if isinstance(error_code, int) else response.status_code
            )
            if (
                retry_safe
                and transient_code in TRANSIENT_TELEGRAM_STATUS_CODES
                and attempt + 1 < attempts
            ):
                parameters = data.get("parameters")
                retry_after = (
                    parameters.get("retry_after")
                    if isinstance(parameters, dict)
                    else None
                )
                if isinstance(retry_after, (int, float)):
                    delay = min(5.0, max(0.0, float(retry_after)))
                else:
                    delay = self._retry_delay(response, attempt)
                await asyncio.sleep(delay)
                continue

            if response.status_code >= 400 or data.get("ok") is False:
                description = data.get("description") or f"Telegram returned {response.status_code}"
                return TelegramResult(ok=False, error=str(description), data=data)

            if data.get("ok") is not True:
                return TelegramResult(
                    ok=False,
                    error="Telegram returned an invalid response.",
                    data=data,
                )

            result = data.get("result")
            message_id: str | None = None
            if isinstance(result, dict) and result.get("message_id") is not None:
                message_id = str(result["message_id"])
            return TelegramResult(ok=True, message_id=message_id, data=data)

        return TelegramResult(ok=False, error="Telegram request failed.")

    def build_visit_message(self, visit: dict[str, Any]) -> str:
        screen = visit.get("screen") or {}
        connection = visit.get("connection") or {}
        screen_text = (
            f"{screen.get('width', 0)}x{screen.get('height', 0)} "
            f"@{screen.get('devicePixelRatio', 1)}x"
        )
        lines = [
            "🌐 <b>Website Visit Alert</b>",
            "",
            f"📅 <b>Time:</b> {self._text(visit.get('local_time') or visit.get('timestamp'))}",
            f"🔗 <b>URL:</b> {self._text(visit.get('url'), maximum=1200)}",
            f"↪️ <b>Referrer:</b> {self._text(visit.get('referrer'), 'Direct visit', 1200)}",
            f"📱 <b>Device:</b> {self._text(visit.get('device'))}",
            f"💻 <b>Browser:</b> {self._text(visit.get('browser'))}",
            f"🖥️ <b>Platform:</b> {self._text(visit.get('platform'))}",
            f"📺 <b>Screen:</b> {self._text(screen_text)}",
            f"📐 <b>Viewport:</b> {self._text(visit.get('viewport'))}",
            f"🌍 <b>Language:</b> {self._text(visit.get('language'))}",
            f"🕒 <b>Timezone:</b> {self._text(visit.get('timezone'))}",
            f"📍 <b>Location:</b> {self._text(visit.get('location'))}",
            f"📶 <b>Network:</b> {self._text(connection.get('effectiveType'))}",
            f"🔐 <b>Visitor:</b> {self._text(visit.get('visitor_id'))} / "
            f"{self._text(visit.get('masked_ip'))}",
            "",
            "👤 A user opened the donation website.",
        ]
        message = "\n".join(lines)
        return message if len(message) <= 3900 else f"{message[:3899]}…"

    async def send_message(
        self,
        chat_id: str | int,
        text: str,
        *,
        reply_to_message_id: int | None = None,
        reply_markup: dict[str, Any] | None = None,
    ) -> TelegramResult:
        payload: dict[str, Any] = {
            "chat_id": str(chat_id),
            "text": text[:4096],
            "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True},
        }
        if reply_to_message_id is not None:
            payload["reply_parameters"] = {"message_id": reply_to_message_id}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        # sendMessage is not retried after ambiguous network failures because
        # Telegram has no client idempotency key and a retry could duplicate it.
        return await self._call("sendMessage", payload)

    async def edit_message_text(
        self,
        chat_id: str | int,
        message_id: int,
        text: str,
        *,
        reply_markup: dict[str, Any] | None = None,
    ) -> TelegramResult:
        payload: dict[str, Any] = {
            "chat_id": str(chat_id),
            "message_id": message_id,
            "text": text[:4096],
            "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True},
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        return await self._call("editMessageText", payload, retry_safe=True)

    async def answer_callback_query(
        self,
        callback_query_id: str,
        text: str | None = None,
        *,
        show_alert: bool = False,
    ) -> TelegramResult:
        payload: dict[str, Any] = {
            "callback_query_id": callback_query_id,
            "show_alert": show_alert,
        }
        if text:
            payload["text"] = text[:200]
        return await self._call("answerCallbackQuery", payload, retry_safe=True)

    async def send_visit(self, visit: dict[str, Any]) -> TelegramResult:
        if not self.settings.telegram_visit_alert_enabled:
            return TelegramResult(
                ok=False,
                skipped=True,
                error="Telegram visit alerts are not configured.",
            )
        return await self.send_message(
            self.settings.telegram_chat_id,
            self.build_visit_message(visit),
        )

    async def configure_webhook(self) -> TelegramResult:
        return await self._call(
            "setWebhook",
            {
                "url": self.settings.telegram_webhook_url,
                "secret_token": self.settings.telegram_webhook_secret,
                "allowed_updates": ["message", "callback_query"],
                "drop_pending_updates": False,
                "max_connections": self.settings.telegram_webhook_max_connections,
            },
            retry_safe=True,
        )

    async def configure_commands(self) -> TelegramResult:
        return await self._call(
            "setMyCommands",
            {
                "commands": [
                    {"command": "manage", "description": "Open supporter manager"},
                    {"command": "list", "description": "List supporters"},
                    {"command": "add", "description": "Add a new supporter"},
                    {"command": "cancel", "description": "Cancel current action"},
                    {"command": "help", "description": "Show command help"},
                ]
            },
            retry_safe=True,
        )
