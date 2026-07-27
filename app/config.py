from __future__ import annotations

import ipaddress
import re
from functools import lru_cache
from urllib.parse import urlparse

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


SUPPORTERS_CACHE_TTL_SECONDS = 60
SUPPORTERS_STALE_CACHE_SECONDS = 86400
DEFAULT_TRUSTED_PROXY_NETWORKS = (
    "127.0.0.0/8,::1/128,10.0.0.0/8,172.16.0.0/12,"
    "192.168.0.0/16,fc00::/7"
)


def _split_csv(value: str) -> list[str]:
    return [item.strip().rstrip("/") for item in value.split(",") if item.strip()]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_name: str = "Ozo Donation API"
    app_environment: str = "development"
    debug: bool = False
    api_prefix: str = "/api"

    backend_cors_origins_raw: str = Field(
        default="http://localhost:3000,http://127.0.0.1:3000",
        validation_alias="BACKEND_CORS_ORIGINS",
    )
    allowed_hosts_raw: str = Field(default="*", validation_alias="ALLOWED_HOSTS")
    trust_proxy_headers: bool = True
    trusted_proxy_networks_raw: str = Field(
        default=DEFAULT_TRUSTED_PROXY_NETWORKS,
        validation_alias="TRUSTED_PROXY_NETWORKS",
    )
    request_timeout_seconds: float = Field(default=8.0, ge=1.0, le=30.0)
    max_request_body_bytes: int = Field(default=96 * 1024, ge=4096, le=1024 * 1024)

    supabase_url: str = ""
    supabase_secret_key: str = ""
    supporters_admin_key: str = ""
    max_supporters: int = Field(default=100, ge=1, le=500)
    supporters_cache_ttl_seconds: int = Field(
        default=SUPPORTERS_CACHE_TTL_SECONDS,
        ge=0,
        le=3600,
    )
    supporters_stale_cache_seconds: int = Field(
        default=SUPPORTERS_STALE_CACHE_SECONDS,
        ge=0,
        le=604800,
    )

    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_webhook_secret: str = ""
    telegram_admin_user_ids_raw: str = Field(
        default="",
        validation_alias="TELEGRAM_ADMIN_USER_IDS",
    )
    telegram_commands_enabled: bool = False
    telegram_webhook_url: str = ""
    telegram_auto_configure_webhook: bool = False

    visit_alert_enabled: bool = True
    visit_hash_salt: str = "development-only-change-me"
    visit_private_key_b64: str = ""
    require_encrypted_visits: bool = True
    visit_alert_cooldown_minutes: int = Field(default=30, ge=1, le=1440)
    visit_rate_limit_per_minute: int = Field(default=20, ge=1, le=1000)
    require_visit_storage: bool = False

    @field_validator("api_prefix")
    @classmethod
    def normalize_api_prefix(cls, value: str) -> str:
        value = value.strip()
        if not value.startswith("/"):
            value = f"/{value}"
        value = value.rstrip("/")
        return value or "/api"

    @property
    def backend_cors_origins(self) -> list[str]:
        return _split_csv(self.backend_cors_origins_raw)

    @property
    def allowed_hosts(self) -> list[str]:
        values = _split_csv(self.allowed_hosts_raw)
        return values or ["*"]

    @property
    def trusted_proxy_networks(self) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
        networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
        for value in _split_csv(self.trusted_proxy_networks_raw):
            networks.append(ipaddress.ip_network(value, strict=False))
        return tuple(networks)

    @property
    def telegram_admin_user_ids(self) -> frozenset[int]:
        values: set[int] = set()
        for item in _split_csv(self.telegram_admin_user_ids_raw):
            values.add(int(item))
        return frozenset(values)

    @property
    def is_production(self) -> bool:
        return self.app_environment.strip().lower() == "production"

    @property
    def supabase_enabled(self) -> bool:
        return bool(self.supabase_url.strip() and self.supabase_secret_key.strip())

    @property
    def telegram_bot_enabled(self) -> bool:
        return bool(self.telegram_bot_token.strip())

    @property
    def telegram_enabled(self) -> bool:
        """Backward-compatible alias for visit-alert availability."""
        return self.telegram_visit_alert_enabled

    @property
    def telegram_visit_alert_enabled(self) -> bool:
        return bool(
            self.visit_alert_enabled
            and self.telegram_bot_token.strip()
            and self.telegram_chat_id.strip()
        )

    @property
    def telegram_commands_configured(self) -> bool:
        return bool(
            self.telegram_commands_enabled
            and self.telegram_bot_token.strip()
            and self.telegram_chat_id.strip()
            and self.telegram_webhook_secret.strip()
            and self.supabase_enabled
        )

    @model_validator(mode="after")
    def validate_settings(self) -> "Settings":
        placeholder_salts = {
            "",
            "development-only-change-me",
            "replace-with-a-long-random-secret",
        }

        try:
            _ = self.trusted_proxy_networks
        except ValueError as exc:
            raise ValueError("TRUSTED_PROXY_NETWORKS contains an invalid IP network.") from exc

        try:
            _ = self.telegram_admin_user_ids
        except ValueError as exc:
            raise ValueError(
                "TELEGRAM_ADMIN_USER_IDS must contain comma-separated integers."
            ) from exc

        if self.supporters_stale_cache_seconds < self.supporters_cache_ttl_seconds:
            raise ValueError(
                "SUPPORTERS_STALE_CACHE_SECONDS must be greater than or equal to "
                "SUPPORTERS_CACHE_TTL_SECONDS."
            )

        if self.require_visit_storage and not self.supabase_enabled:
            raise ValueError(
                "Supabase settings are required when REQUIRE_VISIT_STORAGE=true."
            )

        if self.telegram_commands_enabled:
            if not self.supabase_enabled:
                raise ValueError(
                    "Supabase settings are required when TELEGRAM_COMMANDS_ENABLED=true."
                )
            if not self.telegram_bot_token.strip() or not self.telegram_chat_id.strip():
                raise ValueError(
                    "TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required when "
                    "TELEGRAM_COMMANDS_ENABLED=true."
                )
            webhook_secret = self.telegram_webhook_secret.strip()
            if not re.fullmatch(r"[A-Za-z0-9_-]{24,256}", webhook_secret):
                raise ValueError(
                    "TELEGRAM_WEBHOOK_SECRET must be 24-256 characters and use only "
                    "letters, numbers, underscores, or hyphens."
                )
            try:
                int(self.telegram_chat_id.strip())
            except ValueError as exc:
                raise ValueError(
                    "TELEGRAM_CHAT_ID must be a numeric Telegram chat ID when commands are enabled."
                ) from exc
            if self.telegram_chat_id.strip().startswith("-") and not self.telegram_admin_user_ids:
                raise ValueError(
                    "TELEGRAM_ADMIN_USER_IDS is required when commands are used in a group chat."
                )

        if self.telegram_auto_configure_webhook:
            if not self.telegram_commands_enabled:
                raise ValueError(
                    "TELEGRAM_COMMANDS_ENABLED must be true when auto-configuring the webhook."
                )
            parsed = urlparse(self.telegram_webhook_url.strip())
            if parsed.scheme != "https" or not parsed.netloc:
                raise ValueError("TELEGRAM_WEBHOOK_URL must be a valid HTTPS URL.")

        if self.is_production:
            if self.visit_hash_salt in placeholder_salts or len(self.visit_hash_salt) < 24:
                raise ValueError(
                    "VISIT_HASH_SALT must be a unique secret of at least 24 characters "
                    "in production."
                )
            if self.require_encrypted_visits and not self.visit_private_key_b64.strip():
                raise ValueError(
                    "VISIT_PRIVATE_KEY_B64 is required when REQUIRE_ENCRYPTED_VISITS=true."
                )
            if self.supporters_admin_key and len(self.supporters_admin_key) < 24:
                raise ValueError(
                    "SUPPORTERS_ADMIN_KEY must contain at least 24 characters in production."
                )
            if "*" in _split_csv(self.trusted_proxy_networks_raw):
                raise ValueError("TRUSTED_PROXY_NETWORKS cannot contain '*' in production.")

        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
