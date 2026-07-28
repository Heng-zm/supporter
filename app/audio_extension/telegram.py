from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import PurePath
import time
import logging
from typing import Any

import httpx

from .audio_validation import mime_from_file_name, normalize_mime_type
from .config import AudioSettings
from .store import AudioNotConfiguredError, AudioStore, AudioStoreError

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class TelegramMedia:
    file_id: str
    file_name: str
    mime_type: str
    file_size: int | None


class TelegramAudioController:
    def __init__(
        self,
        settings: AudioSettings,
        client: httpx.AsyncClient,
        store: AudioStore,
    ) -> None:
        self.settings = settings
        self.client = client
        self.store = store
        self._pending: dict[tuple[str, int], float] = {}
        self._pending_lock = asyncio.Lock()

    @staticmethod
    def _message(update: dict[str, Any]) -> dict[str, Any] | None:
        value = update.get("message") or update.get("edited_message")
        return value if isinstance(value, dict) else None

    @staticmethod
    def _command(message: dict[str, Any]) -> tuple[str, str]:
        raw = str(message.get("text") or message.get("caption") or "").strip()
        if not raw.startswith("/"):
            return "", ""
        first, _, rest = raw.partition(" ")
        name = first[1:].split("@", 1)[0].strip().lower()
        return name, rest.strip()

    @staticmethod
    def _chat_id(message: dict[str, Any]) -> str:
        chat = message.get("chat")
        return str(chat.get("id")) if isinstance(chat, dict) and chat.get("id") is not None else ""

    @staticmethod
    def _user_id(message: dict[str, Any]) -> int:
        sender = message.get("from")
        if not isinstance(sender, dict):
            return 0
        try:
            return int(sender.get("id") or 0)
        except (TypeError, ValueError):
            return 0

    def _authorized(self, message: dict[str, Any]) -> bool:
        chat_id = self._chat_id(message)
        user_id = self._user_id(message)
        if not chat_id or not user_id or chat_id != self.settings.telegram_chat_id:
            return False
        if user_id in self.settings.telegram_admin_user_ids:
            return True

        chat = message.get("chat")
        chat_type = str(chat.get("type") or "") if isinstance(chat, dict) else ""
        return bool(
            self.settings.telegram_allow_owner_private_chat
            and chat_type == "private"
            and chat_id == str(user_id)
        )

    @staticmethod
    def _extract_media(message: dict[str, Any]) -> TelegramMedia | None:
        for key in ("audio", "voice", "document"):
            value = message.get(key)
            if not isinstance(value, dict):
                continue

            file_id = str(value.get("file_id") or "").strip()
            if not file_id:
                continue

            file_name = str(value.get("file_name") or "").strip()
            mime_type = normalize_mime_type(value.get("mime_type"))

            if key == "voice":
                file_name = file_name or "voice.ogg"
                mime_type = mime_type or "audio/ogg"
            elif key == "audio":
                file_name = file_name or "audio.mp3"
                mime_type = mime_type or mime_from_file_name(file_name)
            else:
                extension_mime = mime_from_file_name(file_name)
                if not mime_type.startswith("audio/") and not extension_mime:
                    continue
                mime_type = mime_type or extension_mime

            file_size = None
            if value.get("file_size") is not None:
                try:
                    file_size = int(value["file_size"])
                except (TypeError, ValueError):
                    file_size = None

            return TelegramMedia(
                file_id=file_id,
                file_name=file_name or "audio",
                mime_type=mime_type,
                file_size=file_size,
            )
        return None

    @classmethod
    def _media_from_message_or_reply(
        cls,
        message: dict[str, Any],
    ) -> TelegramMedia | None:
        media = cls._extract_media(message)
        if media is not None:
            return media
        reply = message.get("reply_to_message")
        return cls._extract_media(reply) if isinstance(reply, dict) else None

    async def _send_message(self, chat_id: str, text: str) -> None:
        if not self.settings.telegram_bot_token or not chat_id:
            return
        url = (
            f"https://api.telegram.org/bot{self.settings.telegram_bot_token}"
            "/sendMessage"
        )
        response = await self.client.post(
            url,
            json={
                "chat_id": chat_id,
                "text": text[:4000],
                "disable_web_page_preview": True,
            },
        )
        if response.status_code >= 400:
            raise RuntimeError(
                f"Telegram sendMessage failed with HTTP {response.status_code}."
            )

    async def _safe_send_message(self, chat_id: str, text: str) -> bool:
        try:
            await self._send_message(chat_id, text)
            return True
        except Exception as exc:
            logger.warning("Telegram audio status message failed: %s", exc)
            return False

    async def _set_pending(self, key: tuple[str, int]) -> None:
        now = time.monotonic()
        async with self._pending_lock:
            expired = [
                item_key
                for item_key, expires_at in self._pending.items()
                if expires_at <= now
            ]
            for item_key in expired:
                self._pending.pop(item_key, None)
            self._pending[key] = now + self.settings.pending_ttl_seconds
            if len(self._pending) > 200:
                oldest = sorted(self._pending.items(), key=lambda item: item[1])[:50]
                for item_key, _ in oldest:
                    self._pending.pop(item_key, None)

    async def _consume_pending(self, key: tuple[str, int]) -> bool:
        now = time.monotonic()
        async with self._pending_lock:
            expires_at = self._pending.get(key)
            if expires_at is None:
                return False
            if expires_at <= now:
                self._pending.pop(key, None)
                return False
            self._pending.pop(key, None)
            return True

    async def _cancel_pending(self, key: tuple[str, int]) -> bool:
        async with self._pending_lock:
            return self._pending.pop(key, None) is not None

    async def _download(self, media: TelegramMedia) -> bytes:
        if media.file_size is not None and media.file_size > self.settings.max_bytes:
            raise ValueError(
                f"Telegram file is too large. Maximum: {self.settings.max_bytes} bytes."
            )

        token = self.settings.telegram_bot_token
        response = await self.client.post(
            f"https://api.telegram.org/bot{token}/getFile",
            json={"file_id": media.file_id},
        )
        try:
            payload = response.json()
        except ValueError as exc:
            raise RuntimeError("Telegram getFile returned invalid JSON.") from exc

        if response.status_code >= 400 or payload.get("ok") is not True:
            raise RuntimeError(
                str(payload.get("description") or "Telegram getFile failed.")
            )

        result = payload.get("result")
        if not isinstance(result, dict):
            raise RuntimeError("Telegram getFile returned no file result.")

        try:
            remote_size = int(result.get("file_size") or 0)
        except (TypeError, ValueError):
            remote_size = 0
        if remote_size > self.settings.max_bytes:
            raise ValueError(
                f"Telegram file is too large. Maximum: {self.settings.max_bytes} bytes."
            )

        file_path = str(result.get("file_path") or "").strip().lstrip("/")
        if not file_path or ".." in PurePath(file_path).parts:
            raise RuntimeError("Telegram returned an invalid file path.")

        file_response = await self.client.get(
            f"https://api.telegram.org/file/bot{token}/{file_path}"
        )
        if file_response.status_code >= 400:
            raise RuntimeError(
                f"Telegram file download failed with HTTP {file_response.status_code}."
            )

        data = bytes(file_response.content)
        if len(data) > self.settings.max_bytes:
            raise ValueError(
                f"Downloaded audio is too large. Maximum: {self.settings.max_bytes} bytes."
            )
        return data

    async def _install_media(
        self,
        message: dict[str, Any],
        media: TelegramMedia,
        telegram_update_id: int | None,
    ) -> None:
        chat_id = self._chat_id(message)
        user_id = self._user_id(message)

        if telegram_update_id is not None:
            current = await self.store.get_metadata(force=True)
            if current is not None and current.telegram_update_id == telegram_update_id:
                await self._safe_send_message(
                    chat_id,
                    f"ℹ️ This Telegram update is already active as version {current.version}.",
                )
                return

        await self._safe_send_message(chat_id, "⏳ Downloading and validating the new website audio…")

        data = await self._download(media)
        metadata = await self.store.replace(
            file_name=media.file_name,
            mime_type=media.mime_type,
            data=data,
            uploaded_by=user_id,
            telegram_file_id=media.file_id,
            telegram_update_id=telegram_update_id,
        )

        size_mb = metadata.byte_length / (1024 * 1024)
        await self._safe_send_message(
            chat_id,
            (
                "✅ Website audio updated successfully.\n\n"
                f"File: {metadata.file_name}\n"
                f"Type: {metadata.mime_type}\n"
                f"Size: {size_mb:.2f} MB\n"
                f"Version: {metadata.version}\n\n"
                "The React website will detect the new version automatically."
            ),
        )

    async def _send_status(self, chat_id: str) -> None:
        try:
            metadata = await self.store.get_metadata(force=True)
        except AudioNotConfiguredError as exc:
            await self._send_message(chat_id, f"⚠️ Audio storage is not configured: {exc}")
            return
        except AudioStoreError:
            await self._send_message(chat_id, "❌ Audio storage is temporarily unavailable.")
            return

        if metadata is None:
            await self._send_message(chat_id, "ℹ️ No Telegram-managed website audio is active yet.")
            return

        size_mb = metadata.byte_length / (1024 * 1024)
        await self._send_message(
            chat_id,
            (
                "🎵 Current website audio\n\n"
                f"File: {metadata.file_name}\n"
                f"Type: {metadata.mime_type}\n"
                f"Size: {size_mb:.2f} MB\n"
                f"Version: {metadata.version}\n"
                f"Updated: {metadata.updated_at}"
            ),
        )

    async def handle_update(self, update: dict[str, Any]) -> bool:
        message = self._message(update)
        try:
            telegram_update_id = int(update.get("update_id"))
        except (TypeError, ValueError):
            telegram_update_id = None
        if message is None:
            return False

        command, argument = self._command(message)
        is_audio_command = command == "audio"
        chat_id = self._chat_id(message)
        user_id = self._user_id(message)
        key = (chat_id, user_id)

        if is_audio_command and not self._authorized(message):
            if chat_id == self.settings.telegram_chat_id:
                await self._send_message(chat_id, "⛔ This command is restricted to audio administrators.")
            return True

        if is_audio_command:
            lowered = argument.lower()
            if lowered in {"status", "info"}:
                await self._send_status(chat_id)
                return True
            if lowered == "cancel":
                cancelled = await self._cancel_pending(key)
                await self._send_message(
                    chat_id,
                    "✅ Pending audio update cancelled." if cancelled else "ℹ️ No pending audio update.",
                )
                return True
            if lowered in {"help", "?"}:
                await self._send_message(
                    chat_id,
                    (
                        "Audio commands:\n"
                        "/audio — wait for your next audio file\n"
                        "/audio status — show the active file\n"
                        "/audio cancel — cancel the pending upload\n\n"
                        "You can also send an audio/document with caption /audio, "
                        "or reply /audio to an existing audio message."
                    ),
                )
                return True
            if argument:
                await self._send_message(
                    chat_id,
                    "⚠️ Unknown /audio option. Use /audio help.",
                )
                return True

            media = self._media_from_message_or_reply(message)
            if media is not None:
                try:
                    await self._install_media(message, media, telegram_update_id)
                except (ValueError, AudioStoreError, RuntimeError) as exc:
                    await self._send_message(chat_id, f"❌ Audio update failed: {exc}")
                return True

            await self._set_pending(key)
            await self._send_message(
                chat_id,
                (
                    "🎵 Send the new MP3, WAV, OGG, M4A, AAC, WebM, or FLAC file now.\n"
                    f"Maximum size: {self.settings.max_bytes / (1024 * 1024):.1f} MB.\n"
                    "Use /audio cancel to stop."
                ),
            )
            return True

        if not self._authorized(message):
            return False

        media = self._extract_media(message)
        if media is None:
            return False
        if not await self._consume_pending(key):
            return False

        try:
            await self._install_media(message, media, telegram_update_id)
        except (ValueError, AudioStoreError, RuntimeError) as exc:
            await self._send_message(chat_id, f"❌ Audio update failed: {exc}")
        return True
