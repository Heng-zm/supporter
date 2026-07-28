from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import hashlib
import json
import logging
from pathlib import Path
import secrets
import time
from typing import Any
from urllib.parse import quote

import httpx

from .audio_validation import validate_audio_bytes
from .config import AudioSettings
from .models import AudioMetadata

logger = logging.getLogger(__name__)


class AudioStoreError(RuntimeError):
    def __init__(self, message: str, *, code: str = "audio_storage_error") -> None:
        super().__init__(message)
        self.code = code


class AudioNotConfiguredError(AudioStoreError):
    def __init__(self, message: str) -> None:
        super().__init__(message, code="audio_not_configured")


class AudioVersionChangedError(AudioStoreError):
    def __init__(self, current_version: str) -> None:
        super().__init__(current_version, code="audio_version_changed")


class _ObjectNotFound(AudioStoreError):
    def __init__(self, object_path: str) -> None:
        super().__init__(object_path, code="audio_object_not_found")


class AudioStore:
    _READY_CACHE_SECONDS = 30.0

    def __init__(
        self,
        settings: AudioSettings,
        client: httpx.AsyncClient,
    ) -> None:
        self.settings = settings
        self.client = client
        self._lock = asyncio.Lock()
        self._ready_lock = asyncio.Lock()
        self._metadata_cache: AudioMetadata | None = None
        self._metadata_cache_at = 0.0
        self._audio_cache_version = ""
        self._audio_cache_bytes: bytes | None = None
        self._storage_ready = False
        self._storage_checked_at = 0.0
        self._storage_error_code = ""
        self._storage_error_message = ""

    @property
    def mode(self) -> str:
        return self.settings.resolved_storage_mode

    @property
    def available(self) -> bool:
        return not self.settings.configuration_error and (
            self.mode == "local" or self._storage_ready
        )

    @property
    def storage_ready(self) -> bool:
        return self._storage_ready

    @property
    def storage_error_code(self) -> str:
        return self._storage_error_code

    @property
    def storage_error_message(self) -> str:
        return self._storage_error_message

    def status_snapshot(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "ready": self._storage_ready,
            "errorCode": self._storage_error_code or None,
            "error": self._storage_error_message or None,
            "bucket": self.settings.storage_bucket,
        }

    def _set_ready(self) -> None:
        self._storage_ready = True
        self._storage_checked_at = time.monotonic()
        self._storage_error_code = ""
        self._storage_error_message = ""

    def _set_storage_error(self, exc: AudioStoreError) -> None:
        self._storage_ready = False
        self._storage_checked_at = time.monotonic()
        self._storage_error_code = exc.code
        self._storage_error_message = str(exc)

    def _require_configured(self) -> None:
        error = self.settings.configuration_error
        if error:
            raise AudioNotConfiguredError(error)

    def _supabase_headers(self, *, content_type: str | None = None) -> dict[str, str]:
        headers = {
            "apikey": self.settings.supabase_secret_key,
            "Authorization": f"Bearer {self.settings.supabase_secret_key}",
        }
        if content_type:
            headers["Content-Type"] = content_type
        return headers

    def _supabase_object_url(self, object_path: str) -> str:
        bucket = quote(self.settings.storage_bucket, safe="")
        path = quote(object_path.lstrip("/"), safe="/")
        return f"{self.settings.supabase_url}/storage/v1/object/{bucket}/{path}"

    def _supabase_bucket_url(self) -> str:
        bucket = quote(self.settings.storage_bucket, safe="")
        return f"{self.settings.supabase_url}/storage/v1/bucket/{bucket}"

    def _supabase_bucket_collection_url(self) -> str:
        return f"{self.settings.supabase_url}/storage/v1/bucket"

    @staticmethod
    def _response_payload(response: httpx.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except (ValueError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    @classmethod
    def _response_message(cls, response: httpx.Response) -> str:
        payload = cls._response_payload(response)
        values = [
            payload.get("message"),
            payload.get("error"),
            payload.get("error_description"),
        ]
        message = " ".join(str(value) for value in values if value).strip()
        return message or response.text[:300].strip()

    @classmethod
    def _payload_status_code(cls, response: httpx.Response) -> int | None:
        payload = cls._response_payload(response)
        raw = payload.get("statusCode", payload.get("status_code"))
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    @classmethod
    def _is_bucket_not_found(cls, response: httpx.Response) -> bool:
        message = cls._response_message(response).lower()
        payload_code = cls._payload_status_code(response)
        return (
            response.status_code == 404 or payload_code == 404
        ) and "bucket" in message and "not found" in message

    @classmethod
    def _is_object_not_found(cls, response: httpx.Response) -> bool:
        message = cls._response_message(response).lower()
        payload_code = cls._payload_status_code(response)
        not_found = response.status_code == 404 or payload_code == 404
        return not_found and not cls._is_bucket_not_found(response) and (
            not message
            or "object" in message
            or "not found" in message
            or "not_found" in message
        )

    @classmethod
    def _is_already_exists(cls, response: httpx.Response) -> bool:
        message = cls._response_message(response).lower()
        return response.status_code == 409 or "already exists" in message

    @staticmethod
    def _auth_error(response: httpx.Response) -> bool:
        return response.status_code in {401, 403}

    def _supabase_error(
        self,
        response: httpx.Response,
        *,
        operation: str,
    ) -> AudioStoreError:
        if self._auth_error(response):
            return AudioStoreError(
                "Supabase rejected the storage key. Use SUPABASE_SERVICE_ROLE_KEY, "
                "SUPABASE_SECRET_KEY, or a service-role value in SUPABASE_KEY.",
                code="supabase_auth_failed",
            )
        if self._is_bucket_not_found(response):
            return AudioStoreError(
                f'Supabase Storage bucket "{self.settings.storage_bucket}" was not found.',
                code="supabase_bucket_not_found",
            )
        return AudioStoreError(
            f"Supabase Storage {operation} failed with HTTP {response.status_code}.",
            code="supabase_storage_request_failed",
        )

    async def initialize(self, *, force: bool = True) -> None:
        await self.ensure_ready(force=force)

    async def ensure_ready(self, *, force: bool = False) -> None:
        self._require_configured()

        if self.mode == "local":
            try:
                await asyncio.to_thread(
                    self.settings.local_storage_directory.mkdir,
                    parents=True,
                    exist_ok=True,
                )
            except OSError as exc:
                error = AudioStoreError(
                    f"Local audio directory cannot be created: {exc}",
                    code="local_storage_unavailable",
                )
                self._set_storage_error(error)
                raise error from exc
            self._set_ready()
            return

        now = time.monotonic()
        if (
            not force
            and self._storage_ready
            and now - self._storage_checked_at <= self._READY_CACHE_SECONDS
        ):
            return

        async with self._ready_lock:
            now = time.monotonic()
            if (
                not force
                and self._storage_ready
                and now - self._storage_checked_at <= self._READY_CACHE_SECONDS
            ):
                return

            try:
                response = await self.client.get(
                    self._supabase_bucket_url(),
                    headers=self._supabase_headers(),
                )
            except httpx.RequestError as exc:
                error = AudioStoreError(
                    "Could not connect to Supabase Storage.",
                    code="supabase_network_error",
                )
                self._set_storage_error(error)
                raise error from exc

            if response.status_code < 400:
                self._set_ready()
                return

            if self._is_bucket_not_found(response) and self.settings.auto_create_bucket:
                payload = {
                    "id": self.settings.storage_bucket,
                    "name": self.settings.storage_bucket,
                    "public": False,
                    "file_size_limit": self.settings.max_bytes,
                    "allowed_mime_types": [
                        "audio/mpeg",
                        "audio/wav",
                        "audio/ogg",
                        "audio/mp4",
                        "audio/aac",
                        "audio/webm",
                        "audio/flac",
                    ],
                }
                try:
                    create_response = await self.client.post(
                        self._supabase_bucket_collection_url(),
                        headers=self._supabase_headers(content_type="application/json"),
                        json=payload,
                    )
                except httpx.RequestError as exc:
                    error = AudioStoreError(
                        "Could not create the Supabase Storage bucket.",
                        code="supabase_network_error",
                    )
                    self._set_storage_error(error)
                    raise error from exc

                if create_response.status_code < 400 or self._is_already_exists(
                    create_response
                ):
                    logger.info(
                        "Supabase audio bucket is ready bucket=%s created=%s",
                        self.settings.storage_bucket,
                        create_response.status_code < 400,
                    )
                    self._set_ready()
                    return

                error = self._supabase_error(
                    create_response,
                    operation="bucket creation",
                )
                self._set_storage_error(error)
                raise error

            error = self._supabase_error(response, operation="bucket check")
            self._set_storage_error(error)
            raise error

    def _local_path(self, object_path: str) -> Path:
        clean_parts = [part for part in object_path.split("/") if part]
        if not clean_parts or any(part in {".", ".."} for part in clean_parts):
            raise AudioStoreError(
                "Invalid local audio object path.",
                code="invalid_local_object_path",
            )
        return self.settings.local_storage_directory.joinpath(*clean_parts)

    async def _read_object(self, object_path: str) -> bytes:
        if self.mode == "supabase":
            try:
                response = await self.client.get(
                    self._supabase_object_url(object_path),
                    headers=self._supabase_headers(),
                )
            except httpx.RequestError as exc:
                raise AudioStoreError(
                    "Could not connect to Supabase Storage.",
                    code="supabase_network_error",
                ) from exc

            if self._is_object_not_found(response):
                raise _ObjectNotFound(object_path)
            if response.status_code >= 400:
                raise self._supabase_error(response, operation="read")
            return bytes(response.content)

        path = self._local_path(object_path)
        try:
            return await asyncio.to_thread(path.read_bytes)
        except FileNotFoundError as exc:
            raise _ObjectNotFound(object_path) from exc
        except OSError as exc:
            raise AudioStoreError(
                f"Local audio read failed: {exc}",
                code="local_storage_read_failed",
            ) from exc

    async def _write_object(
        self,
        object_path: str,
        data: bytes,
        content_type: str,
    ) -> None:
        if self.mode == "supabase":
            headers = self._supabase_headers(content_type=content_type)
            headers["x-upsert"] = "true"
            headers["cache-control"] = "0"
            try:
                response = await self.client.post(
                    self._supabase_object_url(object_path),
                    headers=headers,
                    content=data,
                )
            except httpx.RequestError as exc:
                raise AudioStoreError(
                    "Could not connect to Supabase Storage.",
                    code="supabase_network_error",
                ) from exc
            if response.status_code >= 400:
                raise self._supabase_error(response, operation="upload")
            return

        path = self._local_path(object_path)

        def atomic_write() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
            try:
                temporary.write_bytes(data)
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)

        try:
            await asyncio.to_thread(atomic_write)
        except OSError as exc:
            raise AudioStoreError(
                f"Local audio write failed: {exc}",
                code="local_storage_write_failed",
            ) from exc

    async def get_metadata(self, *, force: bool = False) -> AudioMetadata | None:
        await self.ensure_ready()
        now = time.monotonic()
        cache_ttl = self.settings.metadata_cache_seconds

        if (
            not force
            and self._metadata_cache is not None
            and now - self._metadata_cache_at <= cache_ttl
        ):
            return self._metadata_cache

        async with self._lock:
            now = time.monotonic()
            if (
                not force
                and self._metadata_cache is not None
                and now - self._metadata_cache_at <= cache_ttl
            ):
                return self._metadata_cache

            try:
                raw = await self._read_object(self.settings.storage_manifest_path)
            except _ObjectNotFound:
                self._metadata_cache = None
                self._metadata_cache_at = now
                return None

            try:
                value = json.loads(raw.decode("utf-8"))
                if not isinstance(value, dict):
                    raise ValueError("Manifest must be a JSON object.")
                metadata = AudioMetadata.from_manifest(value)
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                raise AudioStoreError(
                    f"Invalid audio manifest: {exc}",
                    code="audio_manifest_invalid",
                ) from exc

            if metadata.byte_length > self.settings.max_bytes:
                raise AudioStoreError(
                    "Stored audio exceeds AUDIO_MAX_BYTES.",
                    code="stored_audio_too_large",
                )

            self._metadata_cache = metadata
            self._metadata_cache_at = now
            return metadata

    async def get_audio(
        self,
        *,
        requested_version: str | None = None,
    ) -> tuple[AudioMetadata, bytes]:
        metadata = await self.get_metadata(force=bool(requested_version))
        if metadata is None:
            raise _ObjectNotFound("No active audio is configured.")

        if requested_version and requested_version != metadata.version:
            raise AudioVersionChangedError(metadata.version)

        if (
            self._audio_cache_version == metadata.version
            and self._audio_cache_bytes is not None
        ):
            return metadata, self._audio_cache_bytes

        async with self._lock:
            if (
                self._audio_cache_version == metadata.version
                and self._audio_cache_bytes is not None
            ):
                return metadata, self._audio_cache_bytes

            data = await self._read_object(metadata.object_path)
            if len(data) != metadata.byte_length:
                raise AudioStoreError(
                    "Stored audio byte length does not match its manifest.",
                    code="audio_size_mismatch",
                )
            actual_hash = hashlib.sha256(data).hexdigest()
            if actual_hash != metadata.sha256:
                raise AudioStoreError(
                    "Stored audio checksum does not match its manifest.",
                    code="audio_checksum_mismatch",
                )

            self._audio_cache_version = metadata.version
            self._audio_cache_bytes = data
            return metadata, data

    async def replace(
        self,
        *,
        file_name: str | None,
        mime_type: str | None,
        data: bytes,
        uploaded_by: int | None,
        telegram_file_id: str | None,
        telegram_update_id: int | None = None,
    ) -> AudioMetadata:
        await self.ensure_ready()
        if not data:
            raise ValueError("The audio file is empty.")
        if len(data) > self.settings.max_bytes:
            raise ValueError(
                f"Audio exceeds the configured limit of {self.settings.max_bytes} bytes."
            )

        if telegram_update_id is not None:
            current = await self.get_metadata(force=True)
            if current is not None and current.telegram_update_id == telegram_update_id:
                return current

        detected_mime, safe_name = validate_audio_bytes(data, mime_type, file_name)
        sha256 = hashlib.sha256(data).hexdigest()
        now = datetime.now(timezone.utc)
        version = f"{now.strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(6)}"
        object_path = f"versions/{version}/{safe_name}"

        metadata = AudioMetadata(
            version=version,
            file_name=safe_name,
            mime_type=detected_mime,
            byte_length=len(data),
            sha256=sha256,
            updated_at=now.isoformat(),
            object_path=object_path,
            uploaded_by=uploaded_by,
            telegram_file_id=telegram_file_id,
            telegram_update_id=telegram_update_id,
        )
        manifest_bytes = json.dumps(
            metadata.to_manifest(),
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")

        async with self._lock:
            # Upload the immutable version first. The manifest is written last,
            # so readers never observe a version that has no binary object.
            await self._write_object(object_path, data, detected_mime)
            await self._write_object(
                self.settings.storage_manifest_path,
                manifest_bytes,
                "application/json",
            )

            self._metadata_cache = metadata
            self._metadata_cache_at = time.monotonic()
            self._audio_cache_version = metadata.version
            self._audio_cache_bytes = data

        logger.info(
            "Website audio replaced version=%s bytes=%s uploaded_by=%s mode=%s",
            metadata.version,
            metadata.byte_length,
            uploaded_by,
            self.mode,
        )
        return metadata


ObjectNotFound = _ObjectNotFound
