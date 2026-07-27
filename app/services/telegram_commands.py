from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from html import escape
from typing import Any, Literal

from pydantic import ValidationError

from app.config import Settings
from app.models import (
    SupporterCreate,
    SupporterUpdate,
    TelegramCallbackQuery,
    TelegramChat,
    TelegramMessage,
    TelegramUpdate,
    TelegramUser,
)
from app.services.supabase import SupabaseError, SupabaseService
from app.services.telegram import TelegramService


logger = logging.getLogger("app.telegram_commands")

PAGE_SIZE = 5
PENDING_ACTION_TTL_SECONDS = 10 * 60

ADD_USAGE = (
    "<b>Add supporter</b>\n"
    "<code>/add Name | Amount | Currency | Message | Avatar URL | Payment method</code>\n\n"
    "Only name and amount are required. Currency defaults to USD.\n"
    "Basic: <code>/add John Doe | 25.00</code>\n"
    "With avatar: <code>/add John Doe | 25.00 | USD | "
    "https://example.com/avatar.jpg | ABA</code>\n"
    "To leave a field empty explicitly, use two separators: <code>| |</code>."
)

MANAGER_HELP = (
    "<b>Supporter manager</b>\n"
    "Use the buttons below to add, list, update, or delete supporters.\n\n"
    "Commands: <code>/manage</code>, <code>/list</code>, <code>/add</code>, "
    "<code>/cancel</code>"
)

UPDATE_USAGE = (
    "Send only the fields you want to change. Separate fields with <code>|</code>.\n"
    "Example:\n"
    "<code>name=New Name | amount=20 | currency=USD | payment=ABA</code>\n\n"
    "Available fields: <code>name</code>, <code>amount</code>, <code>currency</code>, "
    "<code>message</code>, <code>avatar</code>, <code>payment</code>, "
    "<code>visible</code>.\n"
    "Use <code>none</code> to clear message, avatar, or payment."
)


@dataclass(slots=True)
class ParsedAddCommand:
    supporter: SupporterCreate


@dataclass(slots=True)
class PendingAction:
    kind: Literal["add", "update"]
    supporter_id: str | None = None
    return_page: int = 0


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


class PendingActionCache:
    def __init__(
        self,
        ttl_seconds: int = PENDING_ACTION_TTL_SECONDS,
        max_items: int = 1000,
    ) -> None:
        self.ttl_seconds = ttl_seconds
        self.max_items = max_items
        self._items: dict[tuple[int, int], tuple[PendingAction, float]] = {}
        self._lock = asyncio.Lock()

    async def set(self, chat_id: int, user_id: int, action: PendingAction) -> None:
        now = time.monotonic()
        async with self._lock:
            self._remove_expired(now)
            if len(self._items) >= self.max_items:
                oldest = min(self._items, key=lambda key: self._items[key][1])
                self._items.pop(oldest, None)
            self._items[(chat_id, user_id)] = (action, now + self.ttl_seconds)

    async def get(self, chat_id: int, user_id: int) -> PendingAction | None:
        now = time.monotonic()
        async with self._lock:
            value = self._items.get((chat_id, user_id))
            if value is None:
                return None
            action, expires_at = value
            if expires_at <= now:
                self._items.pop((chat_id, user_id), None)
                return None
            return action

    async def clear(self, chat_id: int, user_id: int) -> None:
        async with self._lock:
            self._items.pop((chat_id, user_id), None)

    def _remove_expired(self, now: float) -> None:
        expired = [key for key, (_, expires_at) in self._items.items() if expires_at <= now]
        for key in expired:
            self._items.pop(key, None)


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


def parse_update_fields(text: str) -> SupporterUpdate:
    aliases = {
        "name": "name",
        "amount": "amount",
        "currency": "currency",
        "message": "message",
        "avatar": "avatar_url",
        "avatar_url": "avatar_url",
        "payment": "payment_method",
        "payment_method": "payment_method",
        "visible": "is_visible",
        "is_visible": "is_visible",
    }
    clearable = {"message", "avatar_url", "payment_method"}
    values: dict[str, Any] = {}

    for raw_part in text.split("|"):
        part = raw_part.strip()
        if not part:
            continue
        if "=" not in part:
            raise ValueError("Each update field must use name=value format.")
        raw_name, raw_value = part.split("=", 1)
        field_name = aliases.get(raw_name.strip().lower())
        if field_name is None:
            raise ValueError(f"Unknown update field: {raw_name.strip()}.")
        value = raw_value.strip()

        if field_name == "amount":
            values[field_name] = _parse_amount(value)
        elif field_name == "is_visible":
            normalized = value.lower()
            if normalized in {"true", "yes", "1", "show", "visible"}:
                values[field_name] = True
            elif normalized in {"false", "no", "0", "hide", "hidden"}:
                values[field_name] = False
            else:
                raise ValueError("visible must be true or false.")
        elif field_name in clearable and value.lower() in {"none", "null", "clear"}:
            values[field_name] = None
        else:
            values[field_name] = value

    if not values:
        raise ValueError("At least one update field is required.")

    try:
        return SupporterUpdate.model_validate(values)
    except ValidationError as exc:
        first_error = exc.errors()[0].get("msg", "Invalid supporter update.")
        raise ValueError(str(first_error).replace("Value error, ", "")) from exc


def _supporter_row(payload: SupporterCreate | SupporterUpdate) -> dict[str, Any]:
    row = payload.model_dump(exclude_unset=True)
    if isinstance(row.get("amount"), Decimal):
        row["amount"] = float(row["amount"])
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
            "❌ <b>Supabase rejected the supporter request</b>\n"
            "Run the latest <code>supabase_schema.sql</code>, then check the "
            f"Render logs if it still fails.{suffix}"
        )
    if exc.is_transient:
        return "❌ The supporter database is temporarily unavailable. Please try again."
    return (
        "❌ The supporter operation failed. Check the Render logs and "
        "Supabase configuration, then try again."
    )


def _menu_keyboard() -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "➕ Add supporter", "callback_data": "sp:add"},
                {"text": "📋 Supporter list", "callback_data": "sp:list:0"},
            ]
        ]
    }


def _force_reply(placeholder: str) -> dict[str, Any]:
    return {
        "force_reply": True,
        "selective": True,
        "input_field_placeholder": placeholder[:64],
    }


def _truncate_button_text(value: str, maximum: int = 22) -> str:
    compact = " ".join(value.split())
    return compact if len(compact) <= maximum else f"{compact[: maximum - 1]}…"


def _parse_decimal(value: Any, fallback: Decimal = Decimal("0")) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return fallback


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
        self.pending_actions = PendingActionCache()

    def _authorized_actor(self, chat: TelegramChat, sender: TelegramUser | None) -> bool:
        if sender is None or sender.is_bot:
            return False
        if str(chat.id) != self.settings.telegram_chat_id.strip():
            return False

        admins = self.settings.telegram_admin_user_ids
        if admins:
            return sender.id in admins

        return chat.type == "private" and sender.id == chat.id

    def _authorized(self, message: TelegramMessage) -> bool:
        return self._authorized_actor(message.chat, message.from_user)

    async def _reply(
        self,
        message: TelegramMessage,
        text: str,
        *,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        await self.telegram.send_message(
            message.chat.id,
            text,
            reply_to_message_id=message.message_id,
            reply_markup=reply_markup,
        )

    async def _send_menu(self, message: TelegramMessage) -> None:
        await self._reply(message, MANAGER_HELP, reply_markup=_menu_keyboard())

    async def handle(self, update: TelegramUpdate) -> None:
        if update.callback_query is not None:
            callback = update.callback_query
            if not callback.data or not callback.data.startswith("sp:"):
                return
            if not await self.processed_updates.reserve(update.update_id):
                return
            try:
                await self._handle_callback(callback)
            except Exception:
                await self.processed_updates.release(update.update_id)
                raise
            return

        message = update.message
        if message is None or not message.text:
            return

        raw_text = message.text.strip()
        command = raw_text.split(maxsplit=1)[0].split("@", 1)[0].lower()
        supported_commands = {
            "/add",
            "/help",
            "/start",
            "/manage",
            "/list",
            "/supporters",
            "/cancel",
        }
        sender = message.from_user
        pending = (
            await self.pending_actions.get(message.chat.id, sender.id)
            if sender is not None
            else None
        )
        if command not in supported_commands and pending is None:
            return

        if not await self.processed_updates.reserve(update.update_id):
            return
        try:
            await self._handle_message(update, message, command, pending)
        except Exception:
            await self.processed_updates.release(update.update_id)
            raise

    async def _handle_message(
        self,
        update: TelegramUpdate,
        message: TelegramMessage,
        command: str,
        pending: PendingAction | None,
    ) -> None:
        if str(message.chat.id) != self.settings.telegram_chat_id.strip():
            return
        if not self._authorized(message):
            await self._reply(message, "⛔ You are not authorized to use this command.")
            return

        sender = message.from_user
        assert sender is not None

        if command == "/cancel":
            await self.pending_actions.clear(message.chat.id, sender.id)
            await self._reply(message, "✅ Current action cancelled.", reply_markup=_menu_keyboard())
            return

        if command in {"/help", "/start", "/manage"}:
            await self.pending_actions.clear(message.chat.id, sender.id)
            await self._send_menu(message)
            return

        if command in {"/list", "/supporters"}:
            await self._send_supporter_list(message, page=0)
            return

        if command == "/add":
            command_parts = (message.text or "").strip().split(maxsplit=1)
            if len(command_parts) == 1:
                await self._begin_add(message, sender)
                return
            await self._create_from_add_text(update, message, message.text or "")
            return

        if pending is not None:
            await self._handle_pending_input(update, message, sender, pending)

    async def _begin_add(self, message: TelegramMessage, sender: TelegramUser) -> None:
        await self.pending_actions.set(
            message.chat.id,
            sender.id,
            PendingAction(kind="add"),
        )
        await self._reply(
            message,
            "➕ <b>Add supporter</b>\n"
            "Reply with:\n"
            "<code>Name | Amount | Currency | Message | Avatar URL | Payment method</code>\n\n"
            "Only name and amount are required. Send <code>/cancel</code> to stop.",
            reply_markup=_force_reply("Name | Amount | USD | Message | Avatar | ABA"),
        )

    async def _create_from_add_text(
        self,
        update: TelegramUpdate,
        message: TelegramMessage,
        text: str,
    ) -> bool:
        try:
            parsed = parse_add_command(text)
        except ValueError as exc:
            await self._reply(message, f"❌ {escape(str(exc))}\n\n{ADD_USAGE}")
            return False

        if not self.supabase.enabled:
            await self._reply(message, "❌ Supabase is not configured.")
            return False

        try:
            created = await self.supabase.create_supporter_from_telegram(
                _supporter_row(parsed.supporter),
                update.update_id,
            )
        except SupabaseError as exc:
            logger.warning(
                "Telegram supporter add failed: update_id=%s status=%s code=%s",
                update.update_id,
                exc.status_code,
                exc.code,
            )
            await self._reply(message, _database_error_message(exc))
            return False

        await self._reply(
            message,
            self._created_message(created, parsed.supporter),
            reply_markup=_menu_keyboard(),
        )
        return True

    def _created_message(
        self,
        created: dict[str, Any],
        fallback: SupporterCreate,
    ) -> str:
        created_name = escape(str(created.get("name") or fallback.name))
        created_currency = str(created.get("currency") or fallback.currency).upper()
        created_amount = _parse_decimal(created.get("amount"), fallback.amount)
        return (
            "✅ <b>Supporter added</b>\n"
            f"👤 <b>Name:</b> {created_name}\n"
            f"💵 <b>Amount:</b> {escape(_format_amount(created_amount, created_currency))}"
        )

    async def _handle_pending_input(
        self,
        update: TelegramUpdate,
        message: TelegramMessage,
        sender: TelegramUser,
        pending: PendingAction,
    ) -> None:
        text = message.text or ""
        if pending.kind == "add":
            success = await self._create_from_add_text(update, message, f"/add {text}")
            if success:
                await self.pending_actions.clear(message.chat.id, sender.id)
            return

        if pending.kind == "update" and pending.supporter_id:
            try:
                patch = parse_update_fields(text)
            except ValueError as exc:
                await self._reply(
                    message,
                    f"❌ {escape(str(exc))}\n\n{UPDATE_USAGE}\n\n"
                    "Send <code>/cancel</code> to stop.",
                )
                return

            try:
                updated = await self.supabase.update_supporter(
                    pending.supporter_id,
                    _supporter_row(patch),
                )
            except SupabaseError as exc:
                logger.warning(
                    "Telegram supporter update failed: id=%s status=%s code=%s",
                    pending.supporter_id,
                    exc.status_code,
                    exc.code,
                )
                await self._reply(message, _database_error_message(exc))
                return

            if updated is None:
                await self.pending_actions.clear(message.chat.id, sender.id)
                await self._reply(
                    message,
                    "❌ Supporter not found. It may already have been deleted.",
                    reply_markup=_menu_keyboard(),
                )
                return

            await self.pending_actions.clear(message.chat.id, sender.id)
            await self._reply(
                message,
                "✅ <b>Supporter updated</b>\n"
                f"👤 {escape(str(updated.get('name') or 'Unknown'))}",
                reply_markup={
                    "inline_keyboard": [
                        [
                            {
                                "text": "📋 Back to list",
                                "callback_data": f"sp:list:{pending.return_page}",
                            },
                            {"text": "🏠 Menu", "callback_data": "sp:menu"},
                        ]
                    ]
                },
            )

    async def _send_supporter_list(self, message: TelegramMessage, page: int) -> None:
        text, keyboard = await self._supporter_list_view(page)
        await self._reply(message, text, reply_markup=keyboard)

    async def _supporter_list_view(self, page: int) -> tuple[str, dict[str, Any]]:
        page = max(0, page)
        offset = page * PAGE_SIZE
        try:
            rows = await self.supabase.list_supporters_admin(
                limit=PAGE_SIZE + 1,
                offset=offset,
            )
        except SupabaseError as exc:
            return _database_error_message(exc), _menu_keyboard()

        has_next = len(rows) > PAGE_SIZE
        supporters = rows[:PAGE_SIZE]
        lines = [f"📋 <b>Supporter list</b> · Page {page + 1}"]
        keyboard_rows: list[list[dict[str, str]]] = []

        if not supporters:
            lines.extend(["", "No supporters found."])
        else:
            for index, supporter in enumerate(supporters, start=offset + 1):
                supporter_id = str(supporter.get("id") or "")
                name = str(supporter.get("name") or "Unknown")
                currency = str(supporter.get("currency") or "USD").upper()
                amount = _parse_decimal(supporter.get("amount"))
                payment = str(supporter.get("payment_method") or "").strip()
                visible = bool(supporter.get("is_visible", True))
                status_icon = "👁" if visible else "🙈"
                detail = escape(_format_amount(amount, currency))
                if payment:
                    detail += f" · {escape(payment)}"
                lines.extend(
                    [
                        "",
                        f"{index}. {status_icon} <b>{escape(name)}</b>",
                        f"   💵 {detail}",
                    ]
                )
                if supporter_id:
                    keyboard_rows.append(
                        [
                            {
                                "text": f"✏️ {_truncate_button_text(name)}",
                                "callback_data": f"sp:edit:{supporter_id}:{page}",
                            },
                            {
                                "text": "🗑 Delete",
                                "callback_data": f"sp:del:{supporter_id}:{page}",
                            },
                        ]
                    )

        navigation: list[dict[str, str]] = []
        if page > 0:
            navigation.append({"text": "⬅️ Previous", "callback_data": f"sp:list:{page - 1}"})
        if has_next:
            navigation.append({"text": "Next ➡️", "callback_data": f"sp:list:{page + 1}"})
        if navigation:
            keyboard_rows.append(navigation)
        keyboard_rows.append(
            [
                {"text": "➕ Add", "callback_data": "sp:add"},
                {"text": "🔄 Refresh", "callback_data": f"sp:list:{page}"},
                {"text": "🏠 Menu", "callback_data": "sp:menu"},
            ]
        )
        return "\n".join(lines), {"inline_keyboard": keyboard_rows}

    async def _handle_callback(self, callback: TelegramCallbackQuery) -> None:
        message = callback.message
        data = callback.data or ""
        if message is None:
            await self.telegram.answer_callback_query(
                callback.id,
                "This button is no longer available.",
                show_alert=True,
            )
            return
        if str(message.chat.id) != self.settings.telegram_chat_id.strip():
            await self.telegram.answer_callback_query(callback.id)
            return
        if not self._authorized_actor(message.chat, callback.from_user):
            await self.telegram.answer_callback_query(
                callback.id,
                "You are not authorized.",
                show_alert=True,
            )
            return

        parts = data.split(":")
        action = parts[1] if len(parts) > 1 else ""

        if action == "menu":
            await self.pending_actions.clear(message.chat.id, callback.from_user.id)
            await self.telegram.edit_message_text(
                message.chat.id,
                message.message_id,
                MANAGER_HELP,
                reply_markup=_menu_keyboard(),
            )
            await self.telegram.answer_callback_query(callback.id)
            return

        if action == "add":
            await self.pending_actions.set(
                message.chat.id,
                callback.from_user.id,
                PendingAction(kind="add"),
            )
            await self.telegram.send_message(
                message.chat.id,
                "➕ <b>Add supporter</b>\n"
                "Reply with:\n"
                "<code>Name | Amount | Currency | Message | Avatar URL | Payment method</code>\n\n"
                "Only name and amount are required. Send <code>/cancel</code> to stop.",
                reply_markup=_force_reply("Name | Amount | USD | Message | Avatar | ABA"),
            )
            await self.telegram.answer_callback_query(callback.id, "Send the supporter details.")
            return

        if action == "list":
            page = self._page_from_parts(parts, 2)
            text, keyboard = await self._supporter_list_view(page)
            await self.telegram.edit_message_text(
                message.chat.id,
                message.message_id,
                text,
                reply_markup=keyboard,
            )
            await self.telegram.answer_callback_query(callback.id)
            return

        if action == "edit" and len(parts) >= 3:
            supporter_id = parts[2]
            page = self._page_from_parts(parts, 3)
            try:
                supporter = await self.supabase.get_supporter(supporter_id)
            except SupabaseError as exc:
                await self.telegram.answer_callback_query(
                    callback.id,
                    "Database unavailable.",
                    show_alert=True,
                )
                await self.telegram.send_message(message.chat.id, _database_error_message(exc))
                return
            if supporter is None:
                await self.telegram.answer_callback_query(
                    callback.id,
                    "Supporter not found.",
                    show_alert=True,
                )
                return

            await self.pending_actions.set(
                message.chat.id,
                callback.from_user.id,
                PendingAction(kind="update", supporter_id=supporter_id, return_page=page),
            )
            current_name = escape(str(supporter.get("name") or "Unknown"))
            current_currency = str(supporter.get("currency") or "USD").upper()
            current_amount = _parse_decimal(supporter.get("amount"))
            await self.telegram.send_message(
                message.chat.id,
                "✏️ <b>Update supporter</b>\n"
                f"👤 <b>Current:</b> {current_name}\n"
                f"💵 <b>Amount:</b> "
                f"{escape(_format_amount(current_amount, current_currency))}\n\n"
                f"{UPDATE_USAGE}\n\nSend <code>/cancel</code> to stop.",
                reply_markup=_force_reply("name=... | amount=... | payment=..."),
            )
            await self.telegram.answer_callback_query(callback.id, "Send the fields to update.")
            return

        if action == "del" and len(parts) >= 3:
            supporter_id = parts[2]
            page = self._page_from_parts(parts, 3)
            try:
                supporter = await self.supabase.get_supporter(supporter_id)
            except SupabaseError as exc:
                await self.telegram.answer_callback_query(
                    callback.id,
                    "Database unavailable.",
                    show_alert=True,
                )
                await self.telegram.send_message(message.chat.id, _database_error_message(exc))
                return
            if supporter is None:
                await self.telegram.answer_callback_query(
                    callback.id,
                    "Supporter not found.",
                    show_alert=True,
                )
                return
            name = escape(str(supporter.get("name") or "Unknown"))
            await self.telegram.edit_message_text(
                message.chat.id,
                message.message_id,
                "⚠️ <b>Delete supporter?</b>\n\n"
                f"👤 {name}\n"
                "This action cannot be undone.",
                reply_markup={
                    "inline_keyboard": [
                        [
                            {
                                "text": "✅ Yes, delete",
                                "callback_data": f"sp:delok:{supporter_id}:{page}",
                            },
                            {
                                "text": "❌ Cancel",
                                "callback_data": f"sp:list:{page}",
                            },
                        ]
                    ]
                },
            )
            await self.telegram.answer_callback_query(callback.id)
            return

        if action == "delok" and len(parts) >= 3:
            supporter_id = parts[2]
            page = self._page_from_parts(parts, 3)
            try:
                deleted = await self.supabase.delete_supporter(supporter_id)
            except SupabaseError as exc:
                await self.telegram.answer_callback_query(
                    callback.id,
                    "Delete failed.",
                    show_alert=True,
                )
                await self.telegram.send_message(message.chat.id, _database_error_message(exc))
                return
            if not deleted:
                await self.telegram.answer_callback_query(
                    callback.id,
                    "Supporter was already deleted.",
                    show_alert=True,
                )
            else:
                await self.telegram.answer_callback_query(callback.id, "Supporter deleted.")
            text, keyboard = await self._supporter_list_view(page)
            await self.telegram.edit_message_text(
                message.chat.id,
                message.message_id,
                text,
                reply_markup=keyboard,
            )
            return

        await self.telegram.answer_callback_query(
            callback.id,
            "Unknown action.",
            show_alert=True,
        )

    @staticmethod
    def _page_from_parts(parts: list[str], index: int) -> int:
        if len(parts) <= index:
            return 0
        try:
            return max(0, int(parts[index]))
        except ValueError:
            return 0
