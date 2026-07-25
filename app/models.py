from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator


class SupporterBase(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    amount: Decimal = Field(gt=0, le=Decimal("1000000000"), decimal_places=2)
    currency: str = Field(default="USD", min_length=3, max_length=3)
    message: str | None = Field(default=None, max_length=280)
    avatar_url: str | None = Field(
        default=None, max_length=1000, validation_alias=AliasChoices("avatar_url", "avatarUrl")
    )
    payment_method: str | None = Field(
        default=None, max_length=40, validation_alias=AliasChoices("payment_method", "paymentMethod")
    )
    is_visible: bool = Field(
        default=True, validation_alias=AliasChoices("is_visible", "isVisible")
    )

    @field_validator("name", "message", "payment_method", mode="before")
    @classmethod
    def strip_text(cls, value: Any) -> Any:
        if value is None:
            return None
        return str(value).strip()

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value: str) -> str:
        currency = value.strip().upper()
        if not currency.isalpha() or len(currency) != 3:
            raise ValueError("Currency must use a three-letter code.")
        return currency

    @field_validator("avatar_url")
    @classmethod
    def validate_avatar_url(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        value = value.strip()
        if not value.startswith(("http://", "https://")):
            raise ValueError("Avatar URL must begin with http:// or https://.")
        return value


class SupporterCreate(SupporterBase):
    pass


class SupporterUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    amount: Decimal | None = Field(default=None, gt=0, le=Decimal("1000000000"), decimal_places=2)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    message: str | None = Field(default=None, max_length=280)
    avatar_url: str | None = Field(
        default=None, max_length=1000, validation_alias=AliasChoices("avatar_url", "avatarUrl")
    )
    payment_method: str | None = Field(
        default=None, max_length=40, validation_alias=AliasChoices("payment_method", "paymentMethod")
    )
    is_visible: bool | None = Field(
        default=None, validation_alias=AliasChoices("is_visible", "isVisible")
    )

    @field_validator("name", "message", "payment_method", mode="before")
    @classmethod
    def strip_text(cls, value: Any) -> Any:
        if value is None:
            return None
        return str(value).strip()

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value: str | None) -> str | None:
        if value is None:
            return None
        currency = value.strip().upper()
        if not currency.isalpha() or len(currency) != 3:
            raise ValueError("Currency must use a three-letter code.")
        return currency

    @field_validator("avatar_url")
    @classmethod
    def validate_avatar_url(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        value = value.strip()
        if not value.startswith(("http://", "https://")):
            raise ValueError("Avatar URL must begin with http:// or https://.")
        return value

    @model_validator(mode="after")
    def require_one_field(self) -> "SupporterUpdate":
        if not self.model_fields_set:
            raise ValueError("At least one supporter field is required.")
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
    model_config = ConfigDict(extra="ignore")

    event: str = Field(default="website_visit", max_length=80)
    eventId: str | None = Field(
        default=None, max_length=120, validation_alias=AliasChoices("eventId", "event_id")
    )
    timestamp: str | None = Field(default=None, max_length=80)
    localTime: str | None = Field(default=None, max_length=120)
    url: str = Field(default="", max_length=1500)
    path: str = Field(default="/", max_length=500)
    referrer: str = Field(default="Direct visit", max_length=1500)
    title: str = Field(default="Donation Page", max_length=300)
    device: str = Field(
        default="Unknown Device", max_length=120, validation_alias=AliasChoices("device", "deviceName")
    )
    browser: str = Field(default="Unknown Browser", max_length=160)
    platform: str = Field(default="Unknown Platform", max_length=160)
    language: str = Field(default="Unknown", max_length=40)
    timezone: str = Field(default="Unknown", max_length=100)
    userAgent: str = Field(
        default="", max_length=1000, validation_alias=AliasChoices("userAgent", "user_agent")
    )
    screen: ScreenInfo = Field(default_factory=ScreenInfo)
    connection: ConnectionInfo = Field(
        default_factory=ConnectionInfo, validation_alias=AliasChoices("connection", "network")
    )


class VisitResponse(BaseModel):
    ok: bool = True
    duplicate: bool = False
    stored: bool = False
    sent: bool = False
    telegram_skipped: bool = False
