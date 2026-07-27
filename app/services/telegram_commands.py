from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from html import escape
from typing import Any

from pydantic import ValidationError

from app.config import Settings
from app.models import SupporterCreate, TelegramMessage, TelegramUpdate
from app.services.supabase import SupabaseError, SupabaseService
from app.services.telegram import TelegramService


logger = logging.getLogger("app.telegram_commands")


ADD_USAGE = (
    "<b>Add supporter</b>\n"
    "<code>/add Name | Amount | Currency | Message | Avatar URL | Payment method</code>\n\n"
    "Only name and amount are required. Currency defaults to USD.\n"
    "Basic: <code>/add John Doe | 25.00</code>\n"
    "With avatar: <code>/add John Doe | 25.00 | USD | https://example.com/avatar.jpg | ABA</code>\n"
    "To leave a field empty explicitly, use two separators: <code>| |</code>."
)


@dataclass(slots=True)
class ParsedAddCommand:
    supporter: SupporterCreate


class ProcessedUpdateCache:
    def __init__(self, ttl_seconds: int = 86400, max_items: int = 10000) -> None:
        self.ttl_seconds = ttl_seconds
        self.max_items = max_items
        self._items: dict[int, float] = {}
        self._lock = asyncio.Lock()

    async def reserve(self, update_id: int) -> bool:
        now = time.monotonic()
        async with self._lock:
            if len(self._items) >= self.max_items:
                self._items = {
                    key: expires_at
                    for key, expires_at in self._items.items()
                    if expires_at > now
                }
                if len(self._items) >= self.max_items:
                    oldest = min(self._items, key=self._items.get)
                    self._items.pop(oldest, None)
            if self._items.get(update_id, 0) > now:
                return False
            self._items[update_id] = now + self.ttl_seconds
            return True

    async def release(self, update_id: int) -> None:
        async with self._lock:
            self._items.pop(update_id, None)


def _parse_amount(value: str) -> Decimal:
    raw = value.strip()
    if "," in raw:
        valid = re.fullmatch(r"[+-]?\d{1,3}(?:,\d{3})*(?:\.\d+)?", raw)
    else:
        valid = re.fullmatch(r"[+-]?(?:\d+(?:\.\d+)?|\.\d+)", raw)
    if valid is None:
        raise ValueError("Amount must be a valid en-US number.")

    try:
        return Decimal(raw.replace(",", ""))
    except InvalidOperation as exc:
        raise ValueError("Amount must be a valid number.") from exc


def _looks_like_http_url(value: str) -> bool:
    return value.strip().lower().startswith(("http://", "https://"))


def parse_add_command(text: str) -> ParsedAddCommand:
    command_parts = text.strip().split(maxsplit=1)
    command = command_parts[0] if command_parts else ""
    if command.split("@", 1)[0].lower() != "/add":
        raise ValueError("Not an /add command.")
    if len(command_parts) < 2 or not command_parts[1].strip():
        raise ValueError("Name and amount are required.")

    parts = [part.strip() for part in command_parts[1].split("|", 5)]
    if len(parts) < 2:
        raise ValueError("Use | between the supporter name and amount.")

    name = parts[0]
    amount = _parse_amount(parts[1])
    currency = parts[2] if len(parts) > 2 and parts[2] else "USD"

    # Friendly shorthand: when the fourth field is an HTTP(S) URL, treat it
    # as the avatar and consider the optional message omitted. This accepts:
    # /add Name | 1.00 | USD | https://example.com/avatar.jpg | ABA
    # The full positional form with an explicit empty message ("| |") still
    # works and remains unambiguous.
    if len(parts) in {4, 5} and _looks_like_http_url(parts[3]):
        message = None
        avatar_url = parts[3]
        payment_method = parts[4] if len(parts) > 4 and parts[4] else None
    else:
        message = parts[3] if len(parts) > 3 and parts[3] else None
        avatar_url = parts[4] if len(parts) > 4 and parts[4] else None
        payment_method = parts[5] if len(parts) > 5 and parts[5] else None

    try:
        supporter = SupporterCreate(
            name=name,
            amount=amount,
            currency=currency,
            message=message,
            avatar_url=avatar_url,
            payment_method=payment_method,
        )
    except ValidationError as exc:
        first_error = exc.errors()[0].get("msg", "Invalid supporter data.")
        raise ValueError(str(first_error).replace("Value error, ", "")) from exc
    return ParsedAddCommand(supporter=supporter)


def _supporter_row(payload: SupporterCreate) -> dict[str, Any]:
    row = payload.model_dump()
    row["amount"] = float(payload.amount)
    return row


def _format_amount(amount: Decimal, currency: str) -> str:
    number = f"{amount:,.2f}"
    return f"${number}" if currency == "USD" else f"{number} {currency}"


def _database_error_message(exc: SupabaseError) -> str:
    code = (exc.code or "").upper()
    status_code = exc.status_code

    if code == "42P10":
        return (
            "❌ <b>Supabase schema update required</b>\n"
            "Run <code>supabase_migration_v1_3_2.sql</code> in the Supabase "
            "SQL Editor, then send the command again.\n"
            "Error code: <code>42P10</code>"
        )
    if code == "42703":
        return (
            "❌ <b>Supabase supporter columns are outdated</b>\n"
            "Run <code>supabase_schema.sql</code> in the Supabase SQL Editor, "
            "then retry.\n"
            "Error code: <code>42703</code>"
        )
    if code in {"42P01", "PGRST205"} or status_code == 404:
        return (
            "❌ <b>Supabase supporters table was not found</b>\n"
            "Run <code>supabase_schema.sql</code> in the Supabase SQL Editor, "
            "then retry."
        )
    if status_code in {401, 403}:
        return (
            "❌ <b>Supabase access was denied</b>\n"
            "Set <code>SUPABASE_SECRET_KEY</code> to the project service-role "
            "secret on Render, then redeploy."
        )
    if status_code == 400:
        suffix = f"\nError code: <code>{escape(code)}</code>" if code else ""
        return (
            "❌ <b>Supabase rejected the supporter insert</b>\n"
            "Run the latest <code>supabase_schema.sql</code>, then check the "
            f"Render logs if it still fails.{suffix}"
        )
    if exc.is_transient:
        return "❌ The supporter database is temporarily unavailable. Please try again."
    return (
        "❌ The supporter could not be saved. Check the Render logs and "
        "Supabase configuration, then try again."
    )


class TelegramCommandService:
    def __init__(
        self,
        settings: Settings,
        supabase: SupabaseService,
        telegram: TelegramService,
    ) -> None:
        self.settings = settings
        self.supabase = supabase
        self.telegram = telegram
        self.processed_updates = ProcessedUpdateCache()

    def _authorized(self, message: TelegramMessage) -> bool:
        sender = message.from_user
        if sender is None or sender.is_bot:
            return False
        if str(message.chat.id) != self.settings.telegram_chat_id.strip():
            return False

        admins = self.settings.telegram_admin_user_ids
        if admins:
            return sender.id in admins

        return message.chat.type == "private" and sender.id == message.chat.id

    async def _reply(self, message: TelegramMessage, text: str) -> None:
        await self.telegram.send_message(
            message.chat.id,
            text,
            reply_to_message_id=message.message_id,
        )

    async def handle(self, update: TelegramUpdate) -> None:
        message = update.message
        if message is None or not message.text:
            return

        command = message.text.strip().split(maxsplit=1)[0].split("@", 1)[0].lower()
        if command not in {"/add", "/help", "/start"}:
            return

        # Reserve only actionable command updates. If an unexpected exception
        # escapes, release the reservation so Telegram's webhook retry can run.
        if not await self.processed_updates.reserve(update.update_id):
            return

        try:
            await self._handle_reserved(update, message, command)
        except Exception:
            await self.processed_updates.release(update.update_id)
            raise

    async def _handle_reserved(
        self,
        update: TelegramUpdate,
        message: TelegramMessage,
        command: str,
    ) -> None:
        if str(message.chat.id) != self.settings.telegram_chat_id.strip():
            # Ignore commands from other chats instead of making the bot reply
            # outside its configured administration channel.
            return

        if not self._authorized(message):
            await self._reply(message, "⛔ You are not authorized to use this command.")
            return

        if command in {"/help", "/start"}:
            await self._reply(message, ADD_USAGE)
            return

        try:
            parsed = parse_add_command(message.text or "")
        except ValueError as exc:
            await self._reply(
                message,
                f"❌ {escape(str(exc))}\n\n{ADD_USAGE}",
            )
            return

        if not self.supabase.enabled:
            await self._reply(message, "❌ Supabase is not configured.")
            return

        try:
            created = await self.supabase.create_supporter_from_telegram(
                _supporter_row(parsed.supporter),
                update.update_id,
            )
        except SupabaseError as exc:
            logger.warning(
                "Telegram /add failed: update_id=%s status=%s code=%s",
                update.update_id,
                exc.status_code,
                exc.code,
            )
            await self._reply(message, _database_error_message(exc))
            return

        created_name = escape(str(created.get("name") or parsed.supporter.name))
        created_currency = str(created.get("currency") or parsed.supporter.currency).upper()
        try:
            created_amount = Decimal(str(created.get("amount", parsed.supporter.amount)))
        except InvalidOperation:
            created_amount = parsed.supporter.amount

        await self._reply(
            message,
            "✅ <b>Supporter added</b>\n"
            f"👤 <b>Name:</b> {created_name}\n"
            f"💵 <b>Amount:</b> {escape(_format_amount(created_amount, created_currency))}",
        )
