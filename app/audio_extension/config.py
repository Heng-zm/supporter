from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path

from .source_settings import (
    AUDIO_ENABLED,
    AUDIO_HTTP_TIMEOUT_SECONDS,
    AUDIO_LOCAL_STORAGE_DIRECTORY,
    AUDIO_MAX_BYTES,
    AUDIO_METADATA_CACHE_SECONDS,
    AUDIO_PENDING_TTL_SECONDS,
    AUDIO_REQUIRE_PERSISTENT_STORAGE,
    AUDIO_STORAGE_BUCKET,
    AUDIO_STORAGE_MANIFEST_PATH,
    AUDIO_STORAGE_MODE,
    AUDIO_TELEGRAM_ALLOW_OWNER_PRIVATE_CHAT,
)


def _parse_admin_ids(raw: str) -> frozenset[int]:
    values: set[int] = set()
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            value = int(item)
        except ValueError as exc:
            raise ValueError("TELEGRAM_ADMIN_USER_IDS must contain numeric IDs.") from exc
        if value <= 0:
            raise ValueError("TELEGRAM_ADMIN_USER_IDS values must be positive.")
        values.add(value)
    return frozenset(values)


def _validate_source_settings() -> None:
    if AUDIO_STORAGE_MODE not in {"auto", "supabase", "local"}:
        raise ValueError(
            "AUDIO_STORAGE_MODE in source_settings.py must be auto, supabase, or local."
        )
    if not AUDIO_STORAGE_BUCKET.strip():
        raise ValueError("AUDIO_STORAGE_BUCKET in source_settings.py cannot be empty.")
    if not AUDIO_STORAGE_MANIFEST_PATH.strip().lstrip("/"):
        raise ValueError(
            "AUDIO_STORAGE_MANIFEST_PATH in source_settings.py cannot be empty."
        )
    if not 1_024 <= AUDIO_MAX_BYTES <= 20_000_000:
        raise ValueError(
            "AUDIO_MAX_BYTES in source_settings.py must be between 1024 and 20000000."
        )
    if not 0 <= AUDIO_METADATA_CACHE_SECONDS <= 300:
        raise ValueError(
            "AUDIO_METADATA_CACHE_SECONDS in source_settings.py must be between 0 and 300."
        )
    if not 60 <= AUDIO_PENDING_TTL_SECONDS <= 3_600:
        raise ValueError(
            "AUDIO_PENDING_TTL_SECONDS in source_settings.py must be between 60 and 3600."
        )
    if not 10 <= AUDIO_HTTP_TIMEOUT_SECONDS <= 180:
        raise ValueError(
            "AUDIO_HTTP_TIMEOUT_SECONDS in source_settings.py must be between 10 and 180."
        )


@dataclass(frozen=True, slots=True)
class AudioSettings:
    enabled: bool
    storage_mode: str
    storage_bucket: str
    storage_manifest_path: str
    local_storage_directory: Path
    max_bytes: int
    metadata_cache_seconds: int
    pending_ttl_seconds: int
    http_timeout_seconds: int
    supabase_url: str
    supabase_secret_key: str
    telegram_bot_token: str
    telegram_chat_id: str
    telegram_admin_user_ids: frozenset[int]
    telegram_allow_owner_private_chat: bool
    app_environment: str
    require_persistent_storage: bool

    @classmethod
    def from_env(cls) -> "AudioSettings":
        """Load secrets from the environment and non-secret audio settings from source."""
        _validate_source_settings()

        environment = os.getenv("APP_ENVIRONMENT", "development").strip().lower()
        mode = AUDIO_STORAGE_MODE.strip().lower()

        return cls(
            enabled=AUDIO_ENABLED,
            storage_mode=mode,
            storage_bucket=AUDIO_STORAGE_BUCKET.strip(),
            storage_manifest_path=AUDIO_STORAGE_MANIFEST_PATH.strip().lstrip("/"),
            local_storage_directory=Path(AUDIO_LOCAL_STORAGE_DIRECTORY).expanduser(),
            max_bytes=AUDIO_MAX_BYTES,
            metadata_cache_seconds=AUDIO_METADATA_CACHE_SECONDS,
            pending_ttl_seconds=AUDIO_PENDING_TTL_SECONDS,
            http_timeout_seconds=AUDIO_HTTP_TIMEOUT_SECONDS,
            supabase_url=os.getenv("SUPABASE_URL", "").strip().rstrip("/"),
            supabase_secret_key=(
                os.getenv("SUPABASE_SECRET_KEY", "").strip()
                or os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
            ),
            telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
            telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", "").strip(),
            telegram_admin_user_ids=_parse_admin_ids(
                os.getenv("TELEGRAM_ADMIN_USER_IDS", "")
            ),
            telegram_allow_owner_private_chat=(
                AUDIO_TELEGRAM_ALLOW_OWNER_PRIVATE_CHAT
            ),
            app_environment=environment,
            require_persistent_storage=AUDIO_REQUIRE_PERSISTENT_STORAGE,
        )

    @property
    def supabase_configured(self) -> bool:
        return bool(self.supabase_url and self.supabase_secret_key)

    @property
    def resolved_storage_mode(self) -> str:
        if self.storage_mode == "auto":
            return "supabase" if self.supabase_configured else "local"
        return self.storage_mode

    @property
    def configuration_error(self) -> str:
        if not self.enabled:
            return "Audio management is disabled."
        if not self.storage_bucket:
            return "AUDIO_STORAGE_BUCKET is empty."
        if not self.storage_manifest_path:
            return "AUDIO_STORAGE_MANIFEST_PATH is empty."
        if self.resolved_storage_mode == "supabase" and not self.supabase_configured:
            return "Supabase audio storage is selected but Supabase is not configured."
        if self.require_persistent_storage and self.resolved_storage_mode != "supabase":
            return (
                "Persistent audio storage is required, but Supabase Storage is not configured."
            )
        return ""
