from __future__ import annotations

from dataclasses import dataclass
from html import escape
from typing import Any

import httpx

from app.config import Settings


@dataclass(slots=True)
class TelegramResult:
    ok: bool
    skipped: bool = False
    message_id: str | None = None
    error: str | None = None


class TelegramService:
    def __init__(self, settings: Settings, client: httpx.AsyncClient) -> None:
        self.settings = settings
        self.client = client

    @staticmethod
    def _text(value: Any, fallback: str = "Unknown", maximum: int = 900) -> str:
        text = str(value if value is not None else "").strip() or fallback
        return escape(text[:maximum], quote=True)

    def build_visit_message(self, visit: dict[str, Any]) -> str:
        screen = visit.get("screen") or {}
        connection = visit.get("connection") or {}
        screen_text = f"{screen.get('width', 0)}x{screen.get('height', 0)} @{screen.get('devicePixelRatio', 1)}x"
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
            f"🔐 <b>Visitor:</b> {self._text(visit.get('visitor_id'))} / {self._text(visit.get('masked_ip'))}",
            "",
            "👤 A user opened the donation website.",
        ]
        message = "\n".join(lines)
        return message if len(message) <= 3900 else f"{message[:3899]}…"

    async def send_visit(self, visit: dict[str, Any]) -> TelegramResult:
        if not self.settings.telegram_enabled:
            return TelegramResult(ok=False, skipped=True, error="Telegram is not configured.")

        url = f"https://api.telegram.org/bot{self.settings.telegram_bot_token}/sendMessage"
        try:
            response = await self.client.post(
                url,
                json={
                    "chat_id": self.settings.telegram_chat_id,
                    "text": self.build_visit_message(visit),
                    "parse_mode": "HTML",
                    "link_preview_options": {"is_disabled": True},
                },
            )
            data = response.json() if response.content else {}
            if response.status_code >= 400 or data.get("ok") is False:
                return TelegramResult(
                    ok=False,
                    error=str(data.get("description") or f"Telegram returned {response.status_code}"),
                )
            message_id = data.get("result", {}).get("message_id")
            return TelegramResult(ok=True, message_id=str(message_id) if message_id is not None else None)
        except (httpx.HTTPError, ValueError) as exc:
            return TelegramResult(ok=False, error=str(exc))
