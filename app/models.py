from __future__ import annotations

import ipaddress
import unicodedata
from datetime import datetime
from decimal import Decimal
from typing import Any
from urllib.parse import urlparse
from uuid import UUID

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_SUPPORTER_AMOUNT = Decimal("1000000000")


def _clean_text(value: Any) -> str:
    normalized = unicodedata.normalize("NFC", str(value))
    cleaned = "".join(
        " " if character in "\r\n\t" else character
        for character in normalized
        if ord(character) >= 32 and ord(character) != 127
    )
    return " ".join(cleaned.split())


def _normalize_optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = _clean_text(value)
    return text or None


def _validate_avatar_url(value: str | None) -> str | None:
    value = _normalize_optional_text(value)
    if value is None:
        return None

    parsed = urlparse(value)
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise ValueError("Avatar URL must use https://.")
    if parsed.username or parsed.password or parsed.fragment:
        raise ValueError("Avatar URL must not contain credentials or a fragment.")

    hostname = parsed.hostname.rstrip(".").lower()
    if hostname == "localhost" or hostname.endswith(".localhost"):
        raise ValueError("Avatar URL must use a public host.")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise ValueError("Avatar URL must use a public host.")
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
        return _clean_text(value)

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
        return _clean_text(value)

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
    def validate_patch(self) -> SupporterUpdate:
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
    type: str = Field(default="Unknown", max_length=40)
    effectiveType: str = Field(default="Unknown", max_length=80)
    downlinkMbps: float | None = Field(default=None, ge=0, le=100000)
    rttMs: int | None = Field(default=None, ge=0, le=600000)
    saveData: bool = False


class NavigationInfo(BaseModel):
    model_config = ConfigDict(extra="ignore")

    type: str = Field(default="Unknown", max_length=40)
    redirectCount: int = Field(default=0, ge=0, le=100)
    durationMs: float | None = Field(default=None, ge=0, le=3600000)
    domContentLoadedMs: float | None = Field(default=None, ge=0, le=3600000)
    loadTimeMs: float | None = Field(default=None, ge=0, le=3600000)
    transferSizeBytes: int | None = Field(default=None, ge=0, le=1000000000)


class DeviceCapabilities(BaseModel):
    model_config = ConfigDict(extra="ignore")

    memoryGb: float | None = Field(default=None, ge=0, le=1024)
    logicalProcessors: int | None = Field(default=None, ge=0, le=1024)
    maxTouchPoints: int = Field(default=0, ge=0, le=100)
    colorDepth: int | None = Field(default=None, ge=0, le=128)
    cookiesEnabled: bool | None = None
    doNotTrack: bool | None = None


class SessionInfo(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: str | None = Field(
        default=None,
        max_length=128,
        validation_alias=AliasChoices("id", "sessionId", "session_id"),
    )
    pageViews: int = Field(default=1, ge=1, le=1000000)
    returningVisitor: bool = False


class CampaignInfo(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    source: str | None = Field(default=None, max_length=120)
    medium: str | None = Field(default=None, max_length=120)
    name: str | None = Field(
        default=None,
        max_length=160,
        validation_alias=AliasChoices("name", "campaign"),
    )
    campaignId: str | None = Field(default=None, max_length=120)
    term: str | None = Field(default=None, max_length=160)
    content: str | None = Field(default=None, max_length=160)


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
    navigation: NavigationInfo = Field(default_factory=NavigationInfo)
    capabilities: DeviceCapabilities = Field(default_factory=DeviceCapabilities)
    session: SessionInfo = Field(default_factory=SessionInfo)
    campaign: CampaignInfo = Field(default_factory=CampaignInfo)


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

    id: int = Field(ge=1, le=2**63 - 1)
    is_bot: bool = False
    first_name: str = Field(default="", max_length=256)
    username: str | None = Field(default=None, max_length=64)


class TelegramChat(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int = Field(ge=-(2**63), le=2**63 - 1)
    type: str = Field(default="private", max_length=32)


class TelegramMessage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    message_id: int = Field(ge=0, le=2**63 - 1)
    from_user: TelegramUser | None = Field(default=None, alias="from")
    chat: TelegramChat
    date: int | None = Field(default=None, ge=0, le=2**63 - 1)
    text: str | None = Field(default=None, max_length=8192)


class TelegramCallbackQuery(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = Field(min_length=1, max_length=256)
    from_user: TelegramUser = Field(alias="from")
    message: TelegramMessage | None = None
    data: str | None = Field(default=None, max_length=256)


class TelegramUpdate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    update_id: int = Field(ge=0, le=2**63 - 1)
    message: TelegramMessage | None = None
    edited_message: TelegramMessage | None = None
    callback_query: TelegramCallbackQuery | None = None
