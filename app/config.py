from __future__ import annotations

import base64
import binascii
import ipaddress
import json
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


def _parse_networks(
    raw_value: str,
    field_name: str,
) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    try:
        for value in _split_csv(raw_value):
            networks.append(ipaddress.ip_network(value, strict=False))
    except ValueError as exc:
        raise ValueError(f"{field_name} contains an invalid IP network.") from exc
    return tuple(networks)


def _validate_origin(value: str, *, production: bool) -> None:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"Invalid CORS origin: {value!r}.")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError(
            "CORS origin must not contain credentials, query, or fragment: "
            f"{value!r}."
        )
    if parsed.path not in {"", "/"}:
        raise ValueError(f"CORS origin must not contain a path: {value!r}.")
    if production and parsed.scheme != "https":
        raise ValueError(f"Production CORS origins must use HTTPS: {value!r}.")


def _jwt_role(value: str) -> str | None:
    parts = value.split(".")
    if len(parts) != 3:
        return None
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)
    try:
        decoded = base64.urlsafe_b64decode(payload.encode("ascii"))
        data = json.loads(decoded.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError, binascii.Error):
        return None
    role = data.get("role") if isinstance(data, dict) else None
    return str(role) if role is not None else None


def _validate_https_service_url(value: str, field_name: str) -> None:
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError(f"{field_name} must be a valid HTTPS URL.")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError(
            f"{field_name} must not contain credentials, query parameters, or fragments."
        )


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        populate_by_name=True,
    )

    app_name: str = "Ozo Donation API"
    app_environment: str = "development"
    debug: bool = False
    api_prefix: str = "/api"
    enable_api_docs: bool = False

    backend_cors_origins_raw: str = Field(
        default="http://localhost:3000,http://127.0.0.1:3000",
        validation_alias="BACKEND_CORS_ORIGINS",
    )
    allowed_hosts_raw: str = Field(default="*", validation_alias="ALLOWED_HOSTS")
    admin_cors_enabled: bool = False

    trust_proxy_headers: bool = True
    trusted_proxy_networks_raw: str = Field(
        default=DEFAULT_TRUSTED_PROXY_NETWORKS,
        validation_alias="TRUSTED_PROXY_NETWORKS",
    )
    enforce_https: bool = True
    security_headers_enabled: bool = True
    request_logging_enabled: bool = True
    hsts_max_age_seconds: int = Field(default=31536000, ge=0, le=63072000)
    hsts_include_subdomains: bool = False
    hsts_preload: bool = False

    request_timeout_seconds: float = Field(default=8.0, ge=1.0, le=30.0)
    max_request_body_bytes: int = Field(default=96 * 1024, ge=4096, le=1024 * 1024)

    supabase_url: str = ""
    supabase_secret_key: str = ""
    supporters_admin_api_enabled: bool = False
    supporters_admin_key: str = ""
    admin_allowed_networks_raw: str = Field(
        default="",
        validation_alias="ADMIN_ALLOWED_NETWORKS",
    )
    admin_rate_limit_per_minute: int = Field(default=20, ge=1, le=300)
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
    telegram_webhook_allowed_networks_raw: str = Field(
        default="",
        validation_alias="TELEGRAM_WEBHOOK_ALLOWED_NETWORKS",
    )
    telegram_webhook_rate_limit_per_minute: int = Field(default=120, ge=10, le=3000)
    telegram_webhook_max_connections: int = Field(default=10, ge=1, le=100)

    visit_alert_enabled: bool = True
    visit_hash_salt: str = "development-only-change-me"
    visit_private_key_b64: str = ""
    require_encrypted_visits: bool = True
    visit_alert_cooldown_minutes: int = Field(default=30, ge=1, le=1440)
    visit_rate_limit_per_minute: int = Field(default=20, ge=1, le=1000)
    visit_store_url_query: bool = False
    visit_detailed_analytics_enabled: bool = True
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
        return _parse_networks(self.trusted_proxy_networks_raw, "TRUSTED_PROXY_NETWORKS")

    @property
    def admin_allowed_networks(self) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
        return _parse_networks(self.admin_allowed_networks_raw, "ADMIN_ALLOWED_NETWORKS")

    @property
    def telegram_webhook_allowed_networks(
        self,
    ) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
        return _parse_networks(
            self.telegram_webhook_allowed_networks_raw,
            "TELEGRAM_WEBHOOK_ALLOWED_NETWORKS",
        )

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
    def validate_settings(self) -> Settings:
        placeholder_salts = {
            "",
            "development-only-change-me",
            "replace-with-a-long-random-secret",
        }

        _ = self.trusted_proxy_networks
        _ = self.admin_allowed_networks
        _ = self.telegram_webhook_allowed_networks

        try:
            _ = self.telegram_admin_user_ids
        except ValueError as exc:
            raise ValueError(
                "TELEGRAM_ADMIN_USER_IDS must contain comma-separated integers."
            ) from exc

        for origin in self.backend_cors_origins:
            if origin == "*":
                if self.is_production:
                    raise ValueError("BACKEND_CORS_ORIGINS cannot contain '*' in production.")
                continue
            _validate_origin(origin, production=self.is_production)

        if self.trust_proxy_headers and not self.trusted_proxy_networks:
            raise ValueError(
                "TRUSTED_PROXY_NETWORKS is required when TRUST_PROXY_HEADERS=true."
            )

        if self.supporters_stale_cache_seconds < self.supporters_cache_ttl_seconds:
            raise ValueError(
                "SUPPORTERS_STALE_CACHE_SECONDS must be greater than or equal to "
                "SUPPORTERS_CACHE_TTL_SECONDS."
            )

        if bool(self.supabase_url.strip()) != bool(self.supabase_secret_key.strip()):
            raise ValueError(
                "SUPABASE_URL and SUPABASE_SECRET_KEY must either both be configured or "
                "both be empty."
            )

        if self.require_visit_storage and not self.supabase_enabled:
            raise ValueError(
                "Supabase settings are required when REQUIRE_VISIT_STORAGE=true."
            )

        if self.supabase_enabled and self.is_production:
            _validate_https_service_url(self.supabase_url.strip(), "SUPABASE_URL")
            key_role = _jwt_role(self.supabase_secret_key.strip())
            if key_role in {"anon", "authenticated"}:
                raise ValueError(
                    "SUPABASE_SECRET_KEY must be a service-role or server secret, not a "
                    "public anonymous/authenticated key."
                )

        if self.supporters_admin_api_enabled:
            if not self.supabase_enabled:
                raise ValueError(
                    "Supabase settings are required when SUPPORTERS_ADMIN_API_ENABLED=true."
                )
            if len(self.supporters_admin_key.strip()) < 32:
                raise ValueError(
                    "SUPPORTERS_ADMIN_KEY must contain at least 32 characters when the "
                    "admin API is enabled."
                )
            if self.is_production and not self.admin_allowed_networks:
                raise ValueError(
                    "ADMIN_ALLOWED_NETWORKS is required for the supporter admin API in production."
                )

        if (
            self.is_production
            and self.telegram_bot_token.strip()
            and not re.fullmatch(
                r"\d+:[A-Za-z0-9_-]{20,}",
                self.telegram_bot_token.strip(),
            )
        ):
            raise ValueError("TELEGRAM_BOT_TOKEN has an invalid format.")

        if self.is_production and self.visit_alert_enabled:
            if not self.telegram_bot_token.strip() or not self.telegram_chat_id.strip():
                raise ValueError(
                    "TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required when "
                    "VISIT_ALERT_ENABLED=true in production."
                )
            try:
                int(self.telegram_chat_id.strip())
            except ValueError as exc:
                raise ValueError("TELEGRAM_CHAT_ID must be numeric.") from exc

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
            if self.is_production and not self.telegram_admin_user_ids:
                raise ValueError(
                    "TELEGRAM_ADMIN_USER_IDS is required for Telegram commands in production."
                )
            if self.is_production and len(webhook_secret) < 32:
                raise ValueError(
                    "TELEGRAM_WEBHOOK_SECRET must contain at least 32 characters in production."
                )

        if self.telegram_auto_configure_webhook:
            if not self.telegram_commands_enabled:
                raise ValueError(
                    "TELEGRAM_COMMANDS_ENABLED must be true when auto-configuring the webhook."
                )
            _validate_https_service_url(
                self.telegram_webhook_url.strip(),
                "TELEGRAM_WEBHOOK_URL",
            )
            webhook_path = urlparse(self.telegram_webhook_url.strip()).path.rstrip("/")
            expected_path = f"{self.api_prefix}/telegram/webhook"
            if webhook_path != expected_path:
                raise ValueError(
                    f"TELEGRAM_WEBHOOK_URL must end with {expected_path}."
                )

        if self.hsts_preload and (
            not self.hsts_include_subdomains or self.hsts_max_age_seconds < 31536000
        ):
            raise ValueError(
                "HSTS_PRELOAD requires HSTS_INCLUDE_SUBDOMAINS=true and a max age of at "
                "least 31536000 seconds."
            )

        if self.is_production:
            if self.debug:
                raise ValueError("DEBUG must be false in production.")
            if not self.enforce_https:
                raise ValueError("ENFORCE_HTTPS must be true in production.")
            if self.allowed_hosts == ["*"] or "*" in self.allowed_hosts:
                raise ValueError("ALLOWED_HOSTS must be explicit in production.")
            for host in self.allowed_hosts:
                if "://" in host or "/" in host or any(char.isspace() for char in host):
                    raise ValueError(f"Invalid ALLOWED_HOSTS value: {host!r}.")
            if "*" in _split_csv(self.trusted_proxy_networks_raw):
                raise ValueError("TRUSTED_PROXY_NETWORKS cannot contain '*' in production.")
            if self.visit_hash_salt in placeholder_salts or len(self.visit_hash_salt) < 32:
                raise ValueError(
                    "VISIT_HASH_SALT must be a unique secret of at least 32 characters "
                    "in production."
                )
            if self.require_encrypted_visits and not self.visit_private_key_b64.strip():
                raise ValueError(
                    "VISIT_PRIVATE_KEY_B64 is required when REQUIRE_ENCRYPTED_VISITS=true."
                )
            if self.supporters_admin_key and len(self.supporters_admin_key.strip()) < 32:
                raise ValueError(
                    "SUPPORTERS_ADMIN_KEY must contain at least 32 characters in production."
                )

            secrets = [
                value
                for value in (
                    self.visit_hash_salt.strip(),
                    self.telegram_webhook_secret.strip(),
                    self.supporters_admin_key.strip(),
                )
                if value
            ]
            if len(secrets) != len(set(secrets)):
                raise ValueError(
                    "VISIT_HASH_SALT, TELEGRAM_WEBHOOK_SECRET, and SUPPORTERS_ADMIN_KEY "
                    "must use different secrets."
                )

        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
