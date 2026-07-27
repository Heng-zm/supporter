from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
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
    "Add and update supporters one question at a time.\n"
    "បញ្ចូល និងកែប្រែព័ត៌មាន "
    "មួយជំហានម្តង។\n\n"
    "Commands: <code>/manage</code>, <code>/list</code>, <code>/add</code>, "
    "<code>/back</code>, <code>/skip</code>, <code>/cancel</code>"
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
    step: str = "name"
    data: dict[str, Any] = field(default_factory=dict)
    original: dict[str, Any] = field(default_factory=dict)
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


ADD_WIZARD_STEPS = (
    "name",
    "amount",
    "currency",
    "message",
    "avatar_url",
    "payment_method",
    "is_visible",
    "confirm",
)
UPDATE_WIZARD_STEPS = ADD_WIZARD_STEPS
OPTIONAL_FIELDS = {"message", "avatar_url", "payment_method"}


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
                await self._handle_callback(update.update_id, callback)
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
            "/skip",
            "/back",
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
            await self._reply(
                message,
                "✅ Current action cancelled.\n"
                "បានបោះបង់សកម្មភាព"
                "បច្ចុប្បន្ន។",
                reply_markup=_menu_keyboard(),
            )
            return

        if command in {"/help", "/start", "/manage"}:
            await self.pending_actions.clear(message.chat.id, sender.id)
            await self._send_menu(message)
            return

        if command in {"/list", "/supporters"}:
            await self.pending_actions.clear(message.chat.id, sender.id)
            await self._send_supporter_list(message, page=0)
            return

        if command == "/add":
            command_parts = (message.text or "").strip().split(maxsplit=1)
            if len(command_parts) == 1:
                await self._begin_add(message, sender)
                return
            success = await self._create_from_add_text(update, message, message.text or "")
            if success:
                await self.pending_actions.clear(message.chat.id, sender.id)
            return

        if pending is None:
            if command in {"/skip", "/back"}:
                await self._reply(
                    message,
                    "ℹ️ No active form. Send <code>/add</code> or open <code>/manage</code>.",
                    reply_markup=_menu_keyboard(),
                )
            return

        if command == "/back":
            await self._wizard_back(message.chat.id, sender.id, pending, reply_to=message)
            return
        if command == "/skip":
            await self._wizard_skip(message.chat.id, sender.id, pending, reply_to=message)
            return

        await self._handle_pending_input(update, message, sender, pending)

    async def _begin_add(self, message: TelegramMessage, sender: TelegramUser) -> None:
        action = PendingAction(
            kind="add",
            step="name",
            data={
                "currency": "USD",
                "message": None,
                "avatar_url": None,
                "payment_method": None,
                "is_visible": True,
            },
        )
        await self.pending_actions.set(message.chat.id, sender.id, action)
        await self._send_wizard_prompt(
            message.chat.id,
            action,
            reply_to_message_id=message.message_id,
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
            "បានបន្ថែមអ្នកគាំទ្ររួចរាល់។\n\n"
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
        text = (message.text or "").strip()
        lowered = text.lower()

        # Keep the previous compact name=value format for experienced admins.
        # The default UI uses the guided wizard, but old commands continue to work.
        if pending.kind == "update" and pending.supporter_id and "=" in text:
            try:
                patch = parse_update_fields(text)
            except ValueError as exc:
                await self._reply(
                    message,
                    f"❌ {escape(str(exc))}\n\nContinue the guided form or send "
                    "<code>/cancel</code> to stop.",
                    reply_markup=self._wizard_keyboard(pending),
                )
                return
            try:
                updated = await self.supabase.update_supporter(
                    pending.supporter_id,
                    _supporter_row(patch),
                )
            except SupabaseError as exc:
                logger.warning(
                    "Telegram supporter compact update failed: id=%s status=%s code=%s",
                    pending.supporter_id,
                    exc.status_code,
                    exc.code,
                )
                await self._reply(message, _database_error_message(exc))
                return
            await self.pending_actions.clear(message.chat.id, sender.id)
            if updated is None:
                await self._reply(
                    message,
                    "❌ Supporter not found. It may already have been deleted.",
                    reply_markup=_menu_keyboard(),
                )
                return
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
            return

        if pending.step == "confirm":
            if lowered in {"save", "yes", "confirm", "ok"}:
                await self._save_wizard(
                    update.update_id,
                    message.chat.id,
                    sender.id,
                    pending,
                    message,
                )
            elif lowered in {"back", "edit"}:
                await self._wizard_back(message.chat.id, sender.id, pending, reply_to=message)
            else:
                await self._reply(
                    message,
                    "ℹ️ Press <b>Save</b> to confirm, or <b>Back</b> to edit.\n"
                    "ចុច Save ដើម្បីរក្សាទុក ឬ Back "
                    "ដើម្បីកែប្រែ។",
                    reply_markup=self._wizard_keyboard(pending),
                )
            return

        if lowered in {"back", "/back"}:
            await self._wizard_back(message.chat.id, sender.id, pending, reply_to=message)
            return

        if lowered in {"skip", "/skip", "-"}:
            await self._wizard_skip(message.chat.id, sender.id, pending, reply_to=message)
            return

        if pending.kind == "update" and lowered in {"keep", "same", "unchanged"}:
            await self._advance_wizard(message.chat.id, sender.id, pending, reply_to=message)
            return

        if pending.kind == "update" and pending.step in OPTIONAL_FIELDS and lowered in {
            "clear",
            "none",
            "null",
        }:
            pending.data[pending.step] = None
            await self._advance_wizard(message.chat.id, sender.id, pending, reply_to=message)
            return

        try:
            pending.data[pending.step] = self._parse_wizard_field(pending.step, text)
        except ValueError as exc:
            await self._reply(
                message,
                f"❌ {escape(str(exc))}\n\nPlease reply again. Send <code>/back</code> or "
                "<code>/cancel</code> when needed.",
                reply_markup=self._wizard_keyboard(pending),
            )
            return

        await self._advance_wizard(message.chat.id, sender.id, pending, reply_to=message)

    def _parse_wizard_field(self, field_name: str, text: str) -> Any:
        value: Any = text.strip()
        if field_name == "amount":
            value = _parse_amount(value)
        elif field_name == "is_visible":
            normalized = value.lower()
            if normalized in {"true", "yes", "1", "show", "visible", "public"}:
                value = True
            elif normalized in {"false", "no", "0", "hide", "hidden", "private"}:
                value = False
            else:
                raise ValueError("Reply visible or hidden, or use the buttons.")

        try:
            validated = SupporterUpdate.model_validate({field_name: value})
        except ValidationError as exc:
            first_error = exc.errors()[0].get("msg", "Invalid value.")
            raise ValueError(str(first_error).replace("Value error, ", "")) from exc
        return getattr(validated, field_name)

    async def _advance_wizard(
        self,
        chat_id: int,
        user_id: int,
        action: PendingAction,
        *,
        reply_to: TelegramMessage | None = None,
    ) -> None:
        steps = ADD_WIZARD_STEPS if action.kind == "add" else UPDATE_WIZARD_STEPS
        try:
            index = steps.index(action.step)
        except ValueError:
            index = 0
        action.step = steps[min(index + 1, len(steps) - 1)]
        await self.pending_actions.set(chat_id, user_id, action)
        await self._send_wizard_prompt(
            chat_id,
            action,
            reply_to_message_id=reply_to.message_id if reply_to else None,
        )

    async def _wizard_skip(
        self,
        chat_id: int,
        user_id: int,
        action: PendingAction,
        *,
        reply_to: TelegramMessage | None = None,
    ) -> None:
        if action.kind == "update":
            await self._advance_wizard(chat_id, user_id, action, reply_to=reply_to)
            return

        defaults: dict[str, Any] = {
            "currency": "USD",
            "message": None,
            "avatar_url": None,
            "payment_method": None,
            "is_visible": True,
        }
        if action.step not in defaults:
            text = "❌ This field is required and cannot be skipped."
            if reply_to:
                await self._reply(reply_to, text, reply_markup=self._wizard_keyboard(action))
            else:
                await self.telegram.send_message(
                    chat_id,
                    text,
                    reply_markup=self._wizard_keyboard(action),
                )
            return
        action.data[action.step] = defaults[action.step]
        await self._advance_wizard(chat_id, user_id, action, reply_to=reply_to)

    async def _wizard_back(
        self,
        chat_id: int,
        user_id: int,
        action: PendingAction,
        *,
        reply_to: TelegramMessage | None = None,
    ) -> None:
        steps = ADD_WIZARD_STEPS if action.kind == "add" else UPDATE_WIZARD_STEPS
        try:
            index = steps.index(action.step)
        except ValueError:
            index = 0
        if index <= 0:
            await self.pending_actions.clear(chat_id, user_id)
            if action.kind == "update":
                text, keyboard = await self._supporter_list_view(action.return_page)
                await self.telegram.send_message(chat_id, text, reply_markup=keyboard)
            else:
                await self.telegram.send_message(
                    chat_id,
                    MANAGER_HELP,
                    reply_markup=_menu_keyboard(),
                )
            return
        action.step = steps[index - 1]
        await self.pending_actions.set(chat_id, user_id, action)
        await self._send_wizard_prompt(
            chat_id,
            action,
            reply_to_message_id=reply_to.message_id if reply_to else None,
        )

    async def _send_wizard_prompt(
        self,
        chat_id: int,
        action: PendingAction,
        *,
        reply_to_message_id: int | None = None,
    ) -> None:
        await self.telegram.send_message(
            chat_id,
            self._wizard_prompt_text(action),
            reply_to_message_id=reply_to_message_id,
            reply_markup=self._wizard_keyboard(action),
        )

    def _wizard_prompt_text(self, action: PendingAction) -> str:
        heading = (
            "➕ <b>Add supporter</b>"
            if action.kind == "add"
            else "✏️ <b>Update supporter</b>"
        )
        if action.step == "confirm":
            changed = ""
            if action.kind == "update":
                patch = self._wizard_update_patch(action)
                changed = (
                    "\n\nℹ️ No values changed yet. You may go Back or save without changes."
                    if not patch
                    else f"\n\n✏️ <b>Changed fields:</b> {escape(', '.join(patch))}"
                )
            return (
                f"{heading}\n"
                "✅ <b>Confirm information</b>\n"
                "ពិនិត្យព័ត៌មានមុនរក្សាទុក។\n\n"
                f"{self._wizard_summary(action.data)}{changed}"
            )

        steps = ADD_WIZARD_STEPS if action.kind == "add" else UPDATE_WIZARD_STEPS
        number = steps.index(action.step) + 1
        total = len(steps) - 1
        current = ""
        if action.kind == "update":
            display_value = self._display_field_value(
                action.step,
                action.data.get(action.step),
            )
            current = f"\nCurrent: <code>{escape(display_value)}</code>"

        prompts = {
            "name": (
                "Reply with the supporter name.\n"
                "ឆ្លើយតបដោយឈ្មោះអ្នកគាំទ្រ។"
            ),
            "amount": (
                "Reply with the amount, for example <code>1.00</code> "
                "or <code>1,250.50</code>."
            ),
            "currency": "Choose USD/KHR below, or reply with a 3-letter currency code.",
            "message": "Reply with a short message, or press Skip/Clear.",
            "avatar_url": (
                "Reply with an avatar URL beginning with http:// or https://, "
                "or press Skip/Clear."
            ),
            "payment_method": (
                "Choose a payment method below, reply with another method, "
                "or press Skip/Clear."
            ),
            "is_visible": "Choose whether this supporter is visible on the public list.",
        }
        labels = {
            "name": "Name / ឈ្មោះ",
            "amount": "Amount / ចំនួនទឹកប្រាក់",
            "currency": "Currency / រូបិយប័ណ្ណ",
            "message": "Message / សារ",
            "avatar_url": "Avatar URL / រូបភាព",
            "payment_method": "Payment method / វិធីបង់ប្រាក់",
            "is_visible": "Visibility / ការបង្ហាញ",
        }
        extra = (
            "\nPress <b>Keep current</b> to leave it unchanged."
            if action.kind == "update"
            else ""
        )
        return (
            f"{heading}\n"
            f"<b>Step {number}/{total}: {labels[action.step]}</b>{current}\n\n"
            f"{prompts[action.step]}{extra}\n\n"
            "Commands: <code>/back</code> · <code>/skip</code> · <code>/cancel</code>"
        )

    def _wizard_keyboard(self, action: PendingAction) -> dict[str, Any]:
        rows: list[list[dict[str, str]]] = []
        step = action.step

        if step == "confirm":
            rows.append(
                [
                    {
                        "text": "✅ Save / រក្សាទុក",
                        "callback_data": "sp:wizard:save",
                    },
                    {
                        "text": "⬅️ Back / ថយក្រោយ",
                        "callback_data": "sp:wizard:back",
                    },
                ]
            )
            rows.append([{"text": "❌ Cancel", "callback_data": "sp:wizard:cancel"}])
            return {"inline_keyboard": rows}

        if step == "currency":
            rows.append(
                [
                    {"text": "🇺🇸 USD", "callback_data": "sp:wizard:value:USD"},
                    {"text": "🇰🇭 KHR", "callback_data": "sp:wizard:value:KHR"},
                ]
            )
        elif step == "payment_method":
            rows.append(
                [
                    {"text": "ABA", "callback_data": "sp:wizard:value:ABA"},
                    {"text": "ACLEDA", "callback_data": "sp:wizard:value:ACLEDA"},
                    {"text": "Cash", "callback_data": "sp:wizard:value:Cash"},
                ]
            )
        elif step == "is_visible":
            rows.append(
                [
                    {"text": "👁 Visible", "callback_data": "sp:wizard:value:true"},
                    {"text": "🙈 Hidden", "callback_data": "sp:wizard:value:false"},
                ]
            )

        if action.kind == "update":
            field_buttons: list[dict[str, str]] = [
                {"text": "➡️ Keep current", "callback_data": "sp:wizard:keep"}
            ]
            if step in OPTIONAL_FIELDS:
                field_buttons.append({"text": "🧹 Clear", "callback_data": "sp:wizard:clear"})
            rows.append(field_buttons)
        elif step in {"currency", "message", "avatar_url", "payment_method", "is_visible"}:
            rows.append([{"text": "⏭ Skip", "callback_data": "sp:wizard:skip"}])

        rows.append(
            [
                {"text": "⬅️ Back", "callback_data": "sp:wizard:back"},
                {"text": "❌ Cancel", "callback_data": "sp:wizard:cancel"},
            ]
        )
        return {"inline_keyboard": rows}

    def _wizard_summary(self, data: dict[str, Any]) -> str:
        currency = str(data.get("currency") or "USD").upper()
        amount = _parse_decimal(data.get("amount"))
        visible = bool(data.get("is_visible", True))
        return "\n".join(
            [
                f"👤 <b>Name:</b> {escape(str(data.get('name') or '—'))}",
                f"💵 <b>Amount:</b> {escape(_format_amount(amount, currency))}",
                f"💬 <b>Message:</b> {escape(str(data.get('message') or '—'))}",
                f"🖼 <b>Avatar:</b> {escape(str(data.get('avatar_url') or '—'))}",
                f"🏦 <b>Payment:</b> {escape(str(data.get('payment_method') or '—'))}",
                f"{('👁' if visible else '🙈')} <b>Visible:</b> {'Yes' if visible else 'No'}",
            ]
        )

    @staticmethod
    def _display_field_value(field_name: str, value: Any) -> str:
        if field_name == "amount" and value is not None:
            return f"{_parse_decimal(value):,.2f}"
        if field_name == "is_visible":
            return "Visible" if bool(value) else "Hidden"
        return str(value) if value not in {None, ""} else "—"

    def _wizard_update_patch(self, action: PendingAction) -> dict[str, Any]:
        patch: dict[str, Any] = {}
        for field_name in (
            "name",
            "amount",
            "currency",
            "message",
            "avatar_url",
            "payment_method",
            "is_visible",
        ):
            if action.data.get(field_name) != action.original.get(field_name):
                patch[field_name] = action.data.get(field_name)
        return patch

    async def _save_wizard(
        self,
        update_id: int,
        chat_id: int,
        user_id: int,
        action: PendingAction,
        reply_to: TelegramMessage | None = None,
    ) -> None:
        if not self.supabase.enabled:
            await self.telegram.send_message(chat_id, "❌ Supabase is not configured.")
            return

        if action.kind == "add":
            try:
                supporter = SupporterCreate.model_validate(action.data)
            except ValidationError as exc:
                first_error = exc.errors()[0].get("msg", "Invalid supporter data.")
                await self.telegram.send_message(
                    chat_id,
                    f"❌ {escape(str(first_error).replace('Value error, ', ''))}",
                    reply_markup=self._wizard_keyboard(action),
                )
                return
            try:
                created = await self.supabase.create_supporter_from_telegram(
                    _supporter_row(supporter),
                    update_id,
                )
            except SupabaseError as exc:
                logger.warning(
                    "Telegram supporter wizard add failed: update_id=%s status=%s code=%s",
                    update_id,
                    exc.status_code,
                    exc.code,
                )
                await self.telegram.send_message(chat_id, _database_error_message(exc))
                return
            await self.pending_actions.clear(chat_id, user_id)
            await self.telegram.send_message(
                chat_id,
                self._created_message(created, supporter),
                reply_to_message_id=reply_to.message_id if reply_to else None,
                reply_markup=_menu_keyboard(),
            )
            return

        if not action.supporter_id:
            await self.pending_actions.clear(chat_id, user_id)
            await self.telegram.send_message(chat_id, "❌ Supporter ID is missing.")
            return

        raw_patch = self._wizard_update_patch(action)
        if not raw_patch:
            await self.pending_actions.clear(chat_id, user_id)
            await self.telegram.send_message(
                chat_id,
                "ℹ️ No changes were made.",
                reply_markup={
                    "inline_keyboard": [
                        [
                            {
                                "text": "📋 Back to list",
                                "callback_data": f"sp:list:{action.return_page}",
                            },
                            {"text": "🏠 Menu", "callback_data": "sp:menu"},
                        ]
                    ]
                },
            )
            return

        try:
            patch = SupporterUpdate.model_validate(raw_patch)
            updated = await self.supabase.update_supporter(
                action.supporter_id,
                _supporter_row(patch),
            )
        except ValidationError as exc:
            first_error = exc.errors()[0].get("msg", "Invalid supporter update.")
            await self.telegram.send_message(
                chat_id,
                f"❌ {escape(str(first_error).replace('Value error, ', ''))}",
                reply_markup=self._wizard_keyboard(action),
            )
            return
        except SupabaseError as exc:
            logger.warning(
                "Telegram supporter wizard update failed: id=%s status=%s code=%s",
                action.supporter_id,
                exc.status_code,
                exc.code,
            )
            await self.telegram.send_message(chat_id, _database_error_message(exc))
            return

        if updated is None:
            await self.pending_actions.clear(chat_id, user_id)
            await self.telegram.send_message(
                chat_id,
                "❌ Supporter not found. It may already have been deleted.",
                reply_markup=_menu_keyboard(),
            )
            return

        await self.pending_actions.clear(chat_id, user_id)
        await self.telegram.send_message(
            chat_id,
            "✅ <b>Supporter updated</b>\n"
            "បានកែប្រែអ្នកគាំទ្ររួចរាល់។\n\n"
            f"👤 {escape(str(updated.get('name') or 'Unknown'))}",
            reply_markup={
                "inline_keyboard": [
                    [
                        {
                            "text": "📋 Back to list",
                            "callback_data": f"sp:list:{action.return_page}",
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

    async def _handle_callback(
        self,
        update_id: int,
        callback: TelegramCallbackQuery,
    ) -> None:
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
            pending = PendingAction(
                kind="add",
                step="name",
                data={
                    "currency": "USD",
                    "message": None,
                    "avatar_url": None,
                    "payment_method": None,
                    "is_visible": True,
                },
            )
            await self.pending_actions.set(message.chat.id, callback.from_user.id, pending)
            await self._send_wizard_prompt(message.chat.id, pending)
            await self.telegram.answer_callback_query(callback.id, "Step 1: reply with the name.")
            return

        if action == "wizard":
            pending = await self.pending_actions.get(message.chat.id, callback.from_user.id)
            if pending is None:
                await self.telegram.answer_callback_query(
                    callback.id,
                    "This form expired. Start again.",
                    show_alert=True,
                )
                return
            operation = parts[2] if len(parts) > 2 else ""
            if operation == "cancel":
                await self.pending_actions.clear(message.chat.id, callback.from_user.id)
                await self.telegram.answer_callback_query(callback.id, "Cancelled.")
                await self.telegram.send_message(
                    message.chat.id,
                    "✅ Current action cancelled.",
                    reply_markup=_menu_keyboard(),
                )
                return
            if operation == "back":
                await self.telegram.answer_callback_query(callback.id)
                await self._wizard_back(message.chat.id, callback.from_user.id, pending)
                return
            if operation == "skip":
                await self.telegram.answer_callback_query(callback.id)
                await self._wizard_skip(message.chat.id, callback.from_user.id, pending)
                return
            if operation == "keep":
                if pending.kind != "update":
                    await self.telegram.answer_callback_query(callback.id, "Not available.")
                    return
                await self.telegram.answer_callback_query(callback.id)
                await self._advance_wizard(message.chat.id, callback.from_user.id, pending)
                return
            if operation == "clear":
                if pending.kind != "update" or pending.step not in OPTIONAL_FIELDS:
                    await self.telegram.answer_callback_query(
                        callback.id,
                        "This field cannot be cleared.",
                    )
                    return
                pending.data[pending.step] = None
                await self.telegram.answer_callback_query(callback.id)
                await self._advance_wizard(message.chat.id, callback.from_user.id, pending)
                return
            if operation == "value" and len(parts) >= 4:
                raw_value = ":".join(parts[3:])
                try:
                    pending.data[pending.step] = self._parse_wizard_field(pending.step, raw_value)
                except ValueError as exc:
                    await self.telegram.answer_callback_query(
                        callback.id,
                        str(exc)[:180],
                        show_alert=True,
                    )
                    return
                await self.telegram.answer_callback_query(callback.id)
                await self._advance_wizard(message.chat.id, callback.from_user.id, pending)
                return
            if operation == "save":
                if pending.step != "confirm":
                    await self.telegram.answer_callback_query(
                        callback.id,
                        "Complete all steps first.",
                    )
                    return
                await self.telegram.answer_callback_query(callback.id, "Saving…")
                await self._save_wizard(
                    update_id,
                    message.chat.id,
                    callback.from_user.id,
                    pending,
                )
                return
            await self.telegram.answer_callback_query(
                callback.id,
                "Unknown form action.",
                show_alert=True,
            )
            return

        if action == "list":
            await self.pending_actions.clear(message.chat.id, callback.from_user.id)
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

            try:
                current = SupporterCreate(
                    name=supporter.get("name"),
                    amount=supporter.get("amount"),
                    currency=supporter.get("currency") or "USD",
                    message=supporter.get("message"),
                    avatar_url=supporter.get("avatar_url"),
                    payment_method=supporter.get("payment_method"),
                    is_visible=supporter.get("is_visible", True),
                ).model_dump()
            except ValidationError:
                await self.telegram.answer_callback_query(
                    callback.id,
                    "Supporter data is invalid.",
                    show_alert=True,
                )
                return

            pending = PendingAction(
                kind="update",
                step="name",
                data=dict(current),
                original=dict(current),
                supporter_id=supporter_id,
                return_page=page,
            )
            await self.pending_actions.set(message.chat.id, callback.from_user.id, pending)
            await self._send_wizard_prompt(message.chat.id, pending)
            await self.telegram.answer_callback_query(callback.id, "Step 1: reply or keep current.")
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
