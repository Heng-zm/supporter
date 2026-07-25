from __future__ import annotations

from functools import lru_cache
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict



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
    request_timeout_seconds: float = Field(default=8.0, ge=1.0, le=30.0)

    supabase_url: str = ""
    supabase_secret_key: str = ""
    supporters_admin_key: str = ""
    max_supporters: int = Field(default=100, ge=1, le=500)

    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    visit_alert_enabled: bool = True
    visit_hash_salt: str = "development-only-change-me"
    visit_alert_cooldown_minutes: int = Field(default=30, ge=1, le=1440)
    require_visit_storage: bool = False


    @property
    def backend_cors_origins(self) -> list[str]:
        return _split_csv(self.backend_cors_origins_raw)

    @property
    def allowed_hosts(self) -> list[str]:
        values = _split_csv(self.allowed_hosts_raw)
        return values or ["*"]

    @property
    def is_production(self) -> bool:
        return self.app_environment.strip().lower() == "production"

    @property
    def supabase_enabled(self) -> bool:
        return bool(self.supabase_url.strip() and self.supabase_secret_key.strip())

    @property
    def telegram_enabled(self) -> bool:
        return bool(
            self.visit_alert_enabled
            and self.telegram_bot_token.strip()
            and self.telegram_chat_id.strip()
        )

    @model_validator(mode="after")
    def validate_production_secrets(self) -> "Settings":
        placeholder_salts = {
            "",
            "development-only-change-me",
            "replace-with-a-long-random-secret",
        }
        if self.is_production:
            if self.visit_hash_salt in placeholder_salts or len(self.visit_hash_salt) < 24:
                raise ValueError("VISIT_HASH_SALT must be a unique secret of at least 24 characters in production.")
            if self.supporters_admin_key and len(self.supporters_admin_key) < 24:
                raise ValueError("SUPPORTERS_ADMIN_KEY must contain at least 24 characters in production.")
        if self.require_visit_storage and not self.supabase_enabled:
            raise ValueError("Supabase settings are required when REQUIRE_VISIT_STORAGE=true.")
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
