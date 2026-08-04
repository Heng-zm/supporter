from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from app.models import MAX_SUPPORTER_AMOUNT


@dataclass(frozen=True, slots=True)
class SupporterCursor:
    amount: Decimal
    created_at: datetime
    supporter_id: UUID

    @property
    def amount_text(self) -> str:
        return format(self.amount, "f")

    @property
    def created_at_text(self) -> str:
        return self.created_at.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    padded = value + "=" * (-len(value) % 4)
    try:
        return base64.b64decode(padded, altchars=b"-_", validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("The supporter cursor is invalid.") from exc


def _parse_values(value: dict[str, Any]) -> SupporterCursor:
    if set(value) != {"v", "amount", "createdAt", "id"} or value.get("v") != 1:
        raise ValueError("The supporter cursor is invalid.")

    try:
        amount = Decimal(str(value["amount"]))
        created_at = datetime.fromisoformat(
            str(value["createdAt"]).replace("Z", "+00:00")
        )
        supporter_id = UUID(str(value["id"]))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("The supporter cursor is invalid.") from exc

    if (
        not amount.is_finite()
        or amount <= 0
        or amount > MAX_SUPPORTER_AMOUNT
        or created_at.tzinfo is None
    ):
        raise ValueError("The supporter cursor is invalid.")

    return SupporterCursor(
        amount=amount,
        created_at=created_at.astimezone(UTC),
        supporter_id=supporter_id,
    )


def supporter_cursor_from_row(row: dict[str, Any]) -> SupporterCursor:
    return _parse_values(
        {
            "v": 1,
            "amount": row.get("amount"),
            "createdAt": row.get("created_at"),
            "id": row.get("id"),
        }
    )


def encode_supporter_cursor(cursor: SupporterCursor, secret: str) -> str:
    payload = json.dumps(
        {
            "v": 1,
            "amount": cursor.amount_text,
            "createdAt": cursor.created_at_text,
            "id": str(cursor.supporter_id),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    signature = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).digest()
    return f"{_b64encode(payload)}.{_b64encode(signature)}"


def decode_supporter_cursor(value: str, secret: str) -> SupporterCursor:
    try:
        payload_value, signature_value = value.strip().split(".", 1)
    except ValueError as exc:
        raise ValueError("The supporter cursor is invalid.") from exc

    payload = _b64decode(payload_value)
    signature = _b64decode(signature_value)
    expected = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).digest()
    if not hmac.compare_digest(signature, expected):
        raise ValueError("The supporter cursor is invalid.")

    try:
        decoded = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("The supporter cursor is invalid.") from exc
    if not isinstance(decoded, dict):
        raise ValueError("The supporter cursor is invalid.")
    return _parse_values(decoded)
