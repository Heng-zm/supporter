from __future__ import annotations

import base64
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

from .source_settings import (
    AUDIO_AUTO_CREATE_BUCKET,
    AUDIO_ENABLED,
    AUDIO_ENCRYPTION_ACTIVE_KEY_VERSION,
    AUDIO_ENCRYPTION_ALGORITHM,
    AUDIO_ENCRYPTION_CHUNK_BYTES,
    AUDIO_ENCRYPTION_ENABLED,
    AUDIO_HISTORY_LIMIT,
    AUDIO_HISTORY_MANIFEST_PATH,
    AUDIO_HTTP_TIMEOUT_SECONDS,
    AUDIO_LOCAL_STORAGE_DIRECTORY,
    AUDIO_MAX_BYTES,
    AUDIO_METADATA_CACHE_SECONDS,
    AUDIO_PENDING_TTL_SECONDS,
    AUDIO_RANGE_REQUESTS_ENABLED,
    AUDIO_REQUIRE_PERSISTENT_STORAGE,
    AUDIO_RESPONSE_CHUNK_BYTES,
    AUDIO_STORAGE_BUCKET,
    AUDIO_STORAGE_MANIFEST_PATH,
    AUDIO_STORAGE_MODE,
    AUDIO_TELEGRAM_ALLOW_OWNER_PRIVATE_CHAT,
)

_KEY_VERSION_RE = re.compile(r"^[A-Za-z0-9._-]{1,32}$")
_ENCRYPTION_ENV_PREFIX = "AUDIO_ENCRYPTION_KEY_"


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


def _decode_jwt_payload(token: str) -> dict[str, Any] | None:
    """Decode a JWT payload only for safe key classification.

    This does not authenticate or trust the token. Supabase still validates the
    credential server-side. The decoded role is used only to reject known client
    keys before making storage requests.
    """

    parts = token.split(".")
    if len(parts) != 3:
        return None
    payload_segment = parts[1]
    padding = "=" * (-len(payload_segment) % 4)
    try:
        raw = base64.urlsafe_b64decode(payload_segment + padding)
        value = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _supabase_key_error(key: str) -> str:
    value = key.strip()
    if not value:
        return ""
    if value.startswith("sb_publishable_"):
        return (
            "SUPABASE publishable keys cannot manage private audio storage. "
            "Use a Supabase secret key or service-role key on the backend."
        )

    payload = _decode_jwt_payload(value)
    role = str(payload.get("role") or "").strip().lower() if payload else ""
    if role in {"anon", "authenticated"}:
        return (
            f'Supabase JWT role "{role}" cannot manage private audio storage. '
            "Use a Supabase secret key or service-role key on the backend."
        )
    return ""


def _decode_encryption_key(raw: str, env_name: str) -> bytes:
    try:
        value = base64.b64decode(raw.strip(), validate=True)
    except (ValueError, base64.binascii.Error) as exc:
        raise ValueError(f"{env_name} must be valid standard Base64.") from exc
    if len(value) != 32:
        raise ValueError(f"{env_name} must decode to exactly 32 bytes.")
    return value


def _load_encryption_keys() -> Mapping[str, bytes]:
    values: dict[str, bytes] = {}
    for env_name, raw in os.environ.items():
        if not env_name.startswith(_ENCRYPTION_ENV_PREFIX) or not raw.strip():
            continue
        suffix = env_name[len(_ENCRYPTION_ENV_PREFIX) :].strip().lower()
        if not _KEY_VERSION_RE.fullmatch(suffix):
            raise ValueError(
                f"{env_name} has an invalid key version suffix. "
                "Use letters, numbers, dot, underscore, or hyphen."
            )
        values[suffix] = _decode_encryption_key(raw, env_name)
    return MappingProxyType(values)


def _validate_source_settings() -> None:
    if AUDIO_STORAGE_MODE not in {"auto", "supabase", "local"}:
        raise ValueError(
            "AUDIO_STORAGE_MODE in source_settings.py must be auto, supabase, or local."
        )
    if not AUDIO_STORAGE_BUCKET.strip():
        raise ValueError("AUDIO_STORAGE_BUCKET in source_settings.py cannot be empty.")
    for name, value in (
        ("AUDIO_STORAGE_MANIFEST_PATH", AUDIO_STORAGE_MANIFEST_PATH),
        ("AUDIO_HISTORY_MANIFEST_PATH", AUDIO_HISTORY_MANIFEST_PATH),
    ):
        clean = value.strip().lstrip("/")
        if not clean or clean.startswith("versions/") or ".." in clean.split("/"):
            raise ValueError(f"{name} in source_settings.py is invalid.")
    if AUDIO_STORAGE_MANIFEST_PATH == AUDIO_HISTORY_MANIFEST_PATH:
        raise ValueError("Current and history manifest paths must be different.")
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
    if AUDIO_ENCRYPTION_ALGORITHM != "AES-256-GCM-CHUNKED":
        raise ValueError("Only AES-256-GCM-CHUNKED is supported.")
    if not _KEY_VERSION_RE.fullmatch(AUDIO_ENCRYPTION_ACTIVE_KEY_VERSION):
        raise ValueError("AUDIO_ENCRYPTION_ACTIVE_KEY_VERSION is invalid.")
    if not 64 * 1024 <= AUDIO_ENCRYPTION_CHUNK_BYTES <= 4 * 1024 * 1024:
        raise ValueError(
            "AUDIO_ENCRYPTION_CHUNK_BYTES must be between 65536 and 4194304."
        )
    if not 1 <= AUDIO_HISTORY_LIMIT <= 100:
        raise ValueError("AUDIO_HISTORY_LIMIT must be between 1 and 100.")
    if not 16 * 1024 <= AUDIO_RESPONSE_CHUNK_BYTES <= 1024 * 1024:
        raise ValueError(
            "AUDIO_RESPONSE_CHUNK_BYTES must be between 16384 and 1048576."
        )


@dataclass(frozen=True, slots=True)
class AudioSettings:
    enabled: bool
    storage_mode: str
    storage_bucket: str
    storage_manifest_path: str
    history_manifest_path: str
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
    auto_create_bucket: bool = True
    encryption_enabled: bool = True
    encryption_algorithm: str = "AES-256-GCM-CHUNKED"
    encryption_active_key_version: str = "v1"
    encryption_chunk_bytes: int = 1_048_576
    encryption_keys: Mapping[str, bytes] = MappingProxyType({})
    history_limit: int = 10
    range_requests_enabled: bool = True
    response_chunk_bytes: int = 64 * 1024

    @classmethod
    def from_env(cls) -> AudioSettings:
        """Load secrets from environment and non-secret settings from source."""
        _validate_source_settings()

        environment = os.getenv("APP_ENVIRONMENT", "development").strip().lower()
        mode = AUDIO_STORAGE_MODE.strip().lower()

        return cls(
            enabled=AUDIO_ENABLED,
            storage_mode=mode,
            storage_bucket=AUDIO_STORAGE_BUCKET.strip(),
            storage_manifest_path=AUDIO_STORAGE_MANIFEST_PATH.strip().lstrip("/"),
            history_manifest_path=AUDIO_HISTORY_MANIFEST_PATH.strip().lstrip("/"),
            local_storage_directory=Path(AUDIO_LOCAL_STORAGE_DIRECTORY).expanduser(),
            max_bytes=AUDIO_MAX_BYTES,
            metadata_cache_seconds=AUDIO_METADATA_CACHE_SECONDS,
            pending_ttl_seconds=AUDIO_PENDING_TTL_SECONDS,
            http_timeout_seconds=AUDIO_HTTP_TIMEOUT_SECONDS,
            supabase_url=(
                os.getenv("SUPABASE_URL", "").strip()
                or os.getenv("SUPABASE_PROJECT_URL", "").strip()
            ).rstrip("/"),
            supabase_secret_key=(
                os.getenv("SUPABASE_SECRET_KEY", "").strip()
                or os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
                or os.getenv("SUPABASE_KEY", "").strip()
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
            auto_create_bucket=AUDIO_AUTO_CREATE_BUCKET,
            encryption_enabled=AUDIO_ENCRYPTION_ENABLED,
            encryption_algorithm=AUDIO_ENCRYPTION_ALGORITHM,
            encryption_active_key_version=(
                AUDIO_ENCRYPTION_ACTIVE_KEY_VERSION.strip().lower()
            ),
            encryption_chunk_bytes=AUDIO_ENCRYPTION_CHUNK_BYTES,
            encryption_keys=_load_encryption_keys(),
            history_limit=AUDIO_HISTORY_LIMIT,
            range_requests_enabled=AUDIO_RANGE_REQUESTS_ENABLED,
            response_chunk_bytes=AUDIO_RESPONSE_CHUNK_BYTES,
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
    def active_encryption_key(self) -> bytes | None:
        return self.encryption_keys.get(self.encryption_active_key_version)

    @property
    def max_ciphertext_bytes(self) -> int:
        # Header allowance plus 4-byte length and 16-byte GCM tag per chunk.
        chunks = (self.max_bytes + self.encryption_chunk_bytes - 1) // (
            self.encryption_chunk_bytes
        )
        return self.max_bytes + chunks * 20 + 64 * 1024

    @property
    def configuration_error(self) -> str:
        if not self.enabled:
            return "Audio management is disabled."
        if not self.storage_bucket:
            return "AUDIO_STORAGE_BUCKET is empty."
        if not self.storage_manifest_path or not self.history_manifest_path:
            return "Audio manifest paths are empty."
        if self.resolved_storage_mode == "supabase" and not self.supabase_configured:
            return "Supabase audio storage is selected but Supabase is not configured."
        if self.resolved_storage_mode == "supabase":
            key_error = _supabase_key_error(self.supabase_secret_key)
            if key_error:
                return key_error
        if self.require_persistent_storage and self.resolved_storage_mode != "supabase":
            return (
                "Persistent audio storage is required, but Supabase Storage is not configured."
            )
        if self.encryption_enabled:
            if self.encryption_algorithm != "AES-256-GCM-CHUNKED":
                return "The configured audio encryption algorithm is unsupported."
            if self.active_encryption_key is None:
                env_name = (
                    _ENCRYPTION_ENV_PREFIX
                    + self.encryption_active_key_version.upper()
                )
                return f"Missing active audio encryption key: {env_name}."
            if any(len(key) != 32 for key in self.encryption_keys.values()):
                return "Every audio encryption key must be exactly 32 bytes."
        return ""
