from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator


MAX_SUPPORTER_AMOUNT = Decimal("1000000000")


def _normalize_optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _validate_avatar_url(value: str | None) -> str | None:
    value = _normalize_optional_text(value)
    if value is None:
        return None
    if not value.startswith(("http://", "https://")):
        raise ValueError("Avatar URL must begin with http:// or https://.")
    return value


class SupporterBase(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    name: str = Field(min_length=1, max_length=80)
    amount: Decimal = Field(gt=0, le=MAX_SUPPORTER_AMOUNT, decimal_places=2)
    currency: str = Field(default="USD", min_length=3, max_length=3)
    message: str | None = Field(default=None, max_length=280)
    avatar_url: str | None = Field(
        default=None,
        max_length=1000,
        validation_alias=AliasChoices("avatar_url", "avatarUrl"),
    )
    payment_method: str | None = Field(
        default=None,
        max_length=40,
        validation_alias=AliasChoices("payment_method", "paymentMethod"),
    )
    is_visible: bool = Field(
        default=True,
        validation_alias=AliasChoices("is_visible", "isVisible"),
    )

    @field_validator("name", mode="before")
    @classmethod
    def strip_required_name(cls, value: Any) -> str:
        if value is None:
            raise ValueError("Name is required.")
        return str(value).strip()

    @field_validator("message", "payment_method", mode="before")
    @classmethod
    def strip_optional_text(cls, value: Any) -> str | None:
        return _normalize_optional_text(value)

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value: str) -> str:
        currency = value.strip().upper()
        if not currency.isalpha() or len(currency) != 3:
            raise ValueError("Currency must use a three-letter code.")
        return currency

    @field_validator("avatar_url", mode="before")
    @classmethod
    def validate_avatar_url(cls, value: Any) -> str | None:
        return _validate_avatar_url(None if value is None else str(value))


class SupporterCreate(SupporterBase):
    pass


class SupporterUpdate(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    name: str | None = Field(default=None, min_length=1, max_length=80)
    amount: Decimal | None = Field(
        default=None,
        gt=0,
        le=MAX_SUPPORTER_AMOUNT,
        decimal_places=2,
    )
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    message: str | None = Field(default=None, max_length=280)
    avatar_url: str | None = Field(
        default=None,
        max_length=1000,
        validation_alias=AliasChoices("avatar_url", "avatarUrl"),
    )
    payment_method: str | None = Field(
        default=None,
        max_length=40,
        validation_alias=AliasChoices("payment_method", "paymentMethod"),
    )
    is_visible: bool | None = Field(
        default=None,
        validation_alias=AliasChoices("is_visible", "isVisible"),
    )

    @field_validator("name", mode="before")
    @classmethod
    def strip_name(cls, value: Any) -> Any:
        if value is None:
            return None
        return str(value).strip()

    @field_validator("message", "payment_method", mode="before")
    @classmethod
    def strip_optional_text(cls, value: Any) -> str | None:
        return _normalize_optional_text(value)

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value: str | None) -> str | None:
        if value is None:
            return None
        currency = value.strip().upper()
        if not currency.isalpha() or len(currency) != 3:
            raise ValueError("Currency must use a three-letter code.")
        return currency

    @field_validator("avatar_url", mode="before")
    @classmethod
    def validate_avatar_url(cls, value: Any) -> str | None:
        return _validate_avatar_url(None if value is None else str(value))

    @model_validator(mode="after")
    def validate_patch(self) -> "SupporterUpdate":
        if not self.model_fields_set:
            raise ValueError("At least one supporter field is required.")

        non_nullable = ("name", "amount", "currency", "is_visible")
        for field_name in non_nullable:
            if field_name in self.model_fields_set and getattr(self, field_name) is None:
                raise ValueError(f"{field_name} cannot be null.")
        return self


class SupporterOut(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: UUID | str
    name: str
    amount: Decimal
    currency: str = "USD"
    message: str | None = None
    avatar_url: str | None = None
    payment_method: str | None = None
    created_at: datetime | str | None = None


class SupportersResponse(BaseModel):
    ok: bool = True
    supporters: list[SupporterOut]
    source: str = "supabase"
    stale: bool = False


class SupporterResponse(BaseModel):
    ok: bool = True
    supporter: SupporterOut


class DeleteResponse(BaseModel):
    ok: bool = True
    deleted: bool = True


class ScreenInfo(BaseModel):
    model_config = ConfigDict(extra="ignore")

    width: int = Field(default=0, ge=0, le=100000)
    height: int = Field(default=0, ge=0, le=100000)
    viewportWidth: int = Field(default=0, ge=0, le=100000)
    viewportHeight: int = Field(default=0, ge=0, le=100000)
    devicePixelRatio: float = Field(default=1, ge=0, le=100)


class ConnectionInfo(BaseModel):
    model_config = ConfigDict(extra="ignore")

    online: bool = True
    effectiveType: str = Field(default="Unknown", max_length=80)
    saveData: bool = False


class VisitPayload(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    event: str = Field(default="website_visit", max_length=80)
    eventId: str | None = Field(
        default=None,
        max_length=120,
        validation_alias=AliasChoices("eventId", "event_id"),
    )
    timestamp: str | None = Field(default=None, max_length=80)
    localTime: str | None = Field(default=None, max_length=120)
    url: str = Field(default="", max_length=1500)
    path: str = Field(default="/", max_length=500)
    referrer: str = Field(default="Direct visit", max_length=1500)
    title: str = Field(default="Donation Page", max_length=300)
    device: str = Field(
        default="Unknown Device",
        max_length=120,
        validation_alias=AliasChoices("device", "deviceName"),
    )
    browser: str = Field(default="Unknown Browser", max_length=160)
    platform: str = Field(default="Unknown Platform", max_length=160)
    language: str = Field(default="Unknown", max_length=40)
    timezone: str = Field(default="Unknown", max_length=100)
    userAgent: str = Field(
        default="",
        max_length=1000,
        validation_alias=AliasChoices("userAgent", "user_agent"),
    )
    screen: ScreenInfo = Field(default_factory=ScreenInfo)
    connection: ConnectionInfo = Field(
        default_factory=ConnectionInfo,
        validation_alias=AliasChoices("connection", "network"),
    )


class EncryptedVisitEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    encryption: str = Field(pattern=r"^rsa-oaep-aes-gcm-v1$")
    encryptedKey: str = Field(min_length=100, max_length=2000)
    iv: str = Field(min_length=16, max_length=64)
    ciphertext: str = Field(min_length=24, max_length=90000)


class VisitResponse(BaseModel):
    ok: bool = True
    duplicate: bool = False
    stored: bool = False
    sent: bool = False
    telegram_skipped: bool = False


class TelegramUser(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int
    is_bot: bool = False
    first_name: str = ""
    username: str | None = None


class TelegramChat(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int
    type: str = "private"


class TelegramMessage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    message_id: int
    from_user: TelegramUser | None = Field(default=None, alias="from")
    chat: TelegramChat
    text: str | None = None


class TelegramCallbackQuery(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    from_user: TelegramUser = Field(alias="from")
    message: TelegramMessage | None = None
    data: str | None = None


class TelegramUpdate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    update_id: int
    message: TelegramMessage | None = None
    edited_message: TelegramMessage | None = None
    callback_query: TelegramCallbackQuery | None = None
