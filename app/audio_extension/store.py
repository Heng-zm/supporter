from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import secrets
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from .audio_validation import validate_audio_bytes
from .config import AudioSettings
from .encryption import AudioEncryptionError, decrypt_audio_stream, encrypt_audio
from .models import AudioHistory, AudioMetadata

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


class AudioHistorySelectionError(AudioStoreError):
    def __init__(self, message: str) -> None:
        super().__init__(message, code="audio_history_selection_invalid")


class _ObjectNotFound(AudioStoreError):
    def __init__(self, object_path: str) -> None:
        super().__init__(object_path, code="audio_object_not_found")


class AudioStore:
    _READY_CACHE_SECONDS = 30.0
    _READY_ERROR_CACHE_SECONDS = 5.0
    _MANIFEST_MAX_BYTES = 64 * 1024
    _HISTORY_MAX_BYTES = 512 * 1024
    _READ_CHUNK_BYTES = 64 * 1024

    def __init__(
        self,
        settings: AudioSettings,
        client: httpx.AsyncClient,
    ) -> None:
        self.settings = settings
        self.client = client
        self._lock = asyncio.Lock()
        self._replace_lock = asyncio.Lock()
        self._ready_lock = asyncio.Lock()
        self._metadata_cache: AudioMetadata | None = None
        self._metadata_cache_loaded = False
        self._metadata_cache_at = 0.0
        self._history_cache: AudioHistory | None = None
        self._history_cache_loaded = False
        self._history_cache_at = 0.0
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
            "encryptionEnabled": self.settings.encryption_enabled,
            "activeKeyVersion": (
                self.settings.encryption_active_key_version
                if self.settings.encryption_enabled
                else None
            ),
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

    def _cached_storage_error(self) -> AudioStoreError:
        return AudioStoreError(
            self._storage_error_message or "Audio storage is temporarily unavailable.",
            code=self._storage_error_code or "audio_storage_error",
        )

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
        except ValueError:
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
        now = time.monotonic()

        if not force and self._storage_checked_at:
            age = now - self._storage_checked_at
            if self._storage_ready and age <= self._READY_CACHE_SECONDS:
                return
            if (
                not self._storage_ready
                and self._storage_error_code
                and age <= self._READY_ERROR_CACHE_SECONDS
            ):
                raise self._cached_storage_error()

        async with self._ready_lock:
            now = time.monotonic()
            if not force and self._storage_checked_at:
                age = now - self._storage_checked_at
                if self._storage_ready and age <= self._READY_CACHE_SECONDS:
                    return
                if (
                    not self._storage_ready
                    and self._storage_error_code
                    and age <= self._READY_ERROR_CACHE_SECONDS
                ):
                    raise self._cached_storage_error()

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
                    "file_size_limit": self.settings.max_ciphertext_bytes,
                    # Include audio MIME values for compatibility with existing
                    # deployments and application/octet-stream for encrypted data.
                    "allowed_mime_types": [
                        "application/octet-stream",
                        "application/json",
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
        clean_parts = [part for part in object_path.replace("\\", "/").split("/") if part]
        if not clean_parts or any(part in {".", ".."} for part in clean_parts):
            raise AudioStoreError(
                "Invalid local audio object path.",
                code="invalid_local_object_path",
            )

        root = self.settings.local_storage_directory.resolve()
        candidate = root.joinpath(*clean_parts).resolve()
        if candidate != root and root not in candidate.parents:
            raise AudioStoreError(
                "Invalid local audio object path.",
                code="invalid_local_object_path",
            )
        return candidate

    @staticmethod
    def _declared_content_length(response: httpx.Response) -> int | None:
        raw = response.headers.get("content-length", "").strip()
        if not raw:
            return None
        try:
            value = int(raw)
        except ValueError:
            return None
        return value if value >= 0 else None

    async def _iter_object(
        self,
        object_path: str,
        *,
        max_bytes: int,
        too_large_code: str,
    ) -> AsyncIterator[bytes]:
        if self.mode == "supabase":
            try:
                async with self.client.stream(
                    "GET",
                    self._supabase_object_url(object_path),
                    headers=self._supabase_headers(),
                ) as response:
                    if response.status_code >= 400:
                        await response.aread()
                        if self._is_object_not_found(response):
                            raise _ObjectNotFound(object_path)
                        raise self._supabase_error(response, operation="read")

                    declared_length = self._declared_content_length(response)
                    if declared_length is not None and declared_length > max_bytes:
                        raise AudioStoreError(
                            "Stored audio object exceeds the allowed size.",
                            code=too_large_code,
                        )

                    total = 0
                    async for chunk in response.aiter_bytes(self._READ_CHUNK_BYTES):
                        if not chunk:
                            continue
                        total += len(chunk)
                        if total > max_bytes:
                            raise AudioStoreError(
                                "Stored audio object exceeds the allowed size.",
                                code=too_large_code,
                            )
                        yield chunk
                return
            except (_ObjectNotFound, AudioStoreError):
                raise
            except httpx.RequestError as exc:
                raise AudioStoreError(
                    "Could not connect to Supabase Storage.",
                    code="supabase_network_error",
                ) from exc

        path = self._local_path(object_path)
        try:
            file_size = await asyncio.to_thread(lambda: path.stat().st_size)
        except (FileNotFoundError, NotADirectoryError) as exc:
            raise _ObjectNotFound(object_path) from exc
        except OSError as exc:
            raise AudioStoreError(
                f"Local audio read failed: {exc}",
                code="local_storage_read_failed",
            ) from exc
        if file_size > max_bytes:
            raise AudioStoreError(
                "Stored audio object exceeds the allowed size.",
                code=too_large_code,
            )

        try:
            file = await asyncio.to_thread(path.open, "rb")
            try:
                total = 0
                while True:
                    chunk = await asyncio.to_thread(file.read, self._READ_CHUNK_BYTES)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        raise AudioStoreError(
                            "Stored audio object exceeds the allowed size.",
                            code=too_large_code,
                        )
                    yield chunk
            finally:
                await asyncio.to_thread(file.close)
        except AudioStoreError:
            raise
        except OSError as exc:
            raise AudioStoreError(
                f"Local audio read failed: {exc}",
                code="local_storage_read_failed",
            ) from exc

    async def _read_object(
        self,
        object_path: str,
        *,
        max_bytes: int,
        too_large_code: str,
    ) -> bytes:
        chunks: list[bytes] = []
        async for chunk in self._iter_object(
            object_path,
            max_bytes=max_bytes,
            too_large_code=too_large_code,
        ):
            chunks.append(chunk)
        return b"".join(chunks)

    async def _write_object(
        self,
        object_path: str,
        data: bytes,
        content_type: str,
        *,
        upsert: bool,
    ) -> None:
        if self.mode == "supabase":
            headers = self._supabase_headers(content_type=content_type)
            if upsert:
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

    async def _delete_object(self, object_path: str) -> None:
        if self.mode == "supabase":
            try:
                response = await self.client.delete(
                    self._supabase_object_url(object_path),
                    headers=self._supabase_headers(),
                )
            except httpx.RequestError as exc:
                raise AudioStoreError(
                    "Could not connect to Supabase Storage.",
                    code="supabase_network_error",
                ) from exc
            if response.status_code >= 400 and not self._is_object_not_found(response):
                raise self._supabase_error(response, operation="delete")
            return

        path = self._local_path(object_path)
        try:
            await asyncio.to_thread(path.unlink, missing_ok=True)
        except OSError as exc:
            raise AudioStoreError(
                f"Local audio delete failed: {exc}",
                code="local_storage_delete_failed",
            ) from exc

    async def _cleanup_orphan(self, object_path: str) -> None:
        try:
            await self._delete_object(object_path)
        except AudioStoreError as exc:
            logger.warning(
                "Failed to clean up orphaned audio object path=%s code=%s",
                object_path,
                exc.code,
            )

    async def _cleanup_pruned_history(
        self,
        previous: AudioHistory,
        retained: AudioHistory,
    ) -> None:
        retained_paths = {item.object_path for item in retained.items}
        for item in previous.items:
            if item.object_path in retained_paths:
                continue
            try:
                await self._delete_object(item.object_path)
            except AudioStoreError as exc:
                logger.warning(
                    "Failed to delete pruned audio version=%s code=%s",
                    item.version,
                    exc.code,
                )

    def _metadata_cache_fresh(self, now: float) -> bool:
        return (
            self._metadata_cache_loaded
            and now - self._metadata_cache_at <= self.settings.metadata_cache_seconds
        )

    def _history_cache_fresh(self, now: float) -> bool:
        return (
            self._history_cache_loaded
            and now - self._history_cache_at <= self.settings.metadata_cache_seconds
        )

    async def get_metadata(self, *, force: bool = False) -> AudioMetadata | None:
        await self.ensure_ready()
        now = time.monotonic()

        if not force and self._metadata_cache_fresh(now):
            return self._metadata_cache

        async with self._lock:
            now = time.monotonic()
            if not force and self._metadata_cache_fresh(now):
                return self._metadata_cache

            try:
                raw = await self._read_object(
                    self.settings.storage_manifest_path,
                    max_bytes=self._MANIFEST_MAX_BYTES,
                    too_large_code="audio_manifest_too_large",
                )
            except _ObjectNotFound:
                self._metadata_cache = None
                self._metadata_cache_loaded = True
                self._metadata_cache_at = now
                self._audio_cache_version = ""
                self._audio_cache_bytes = None
                return None

            try:
                value = json.loads(raw.decode("utf-8"))
                if not isinstance(value, dict):
                    raise ValueError("Manifest must be a JSON object.")
                metadata = AudioMetadata.from_manifest(value)
            except (UnicodeDecodeError, ValueError) as exc:
                raise AudioStoreError(
                    f"Invalid audio manifest: {exc}",
                    code="audio_manifest_invalid",
                ) from exc

            if metadata.byte_length > self.settings.max_bytes:
                raise AudioStoreError(
                    "Stored audio exceeds AUDIO_MAX_BYTES.",
                    code="stored_audio_too_large",
                )
            if (
                metadata.encrypted
                and (metadata.ciphertext_byte_length or 0)
                > self.settings.max_ciphertext_bytes
            ):
                raise AudioStoreError(
                    "Stored encrypted audio exceeds the allowed size.",
                    code="stored_encrypted_audio_too_large",
                )

            self._metadata_cache = metadata
            self._metadata_cache_loaded = True
            self._metadata_cache_at = now
            return metadata

    async def get_history(self, *, force: bool = False) -> AudioHistory:
        await self.ensure_ready()
        now = time.monotonic()
        if not force and self._history_cache_fresh(now) and self._history_cache is not None:
            return self._history_cache

        # Load current metadata before taking the shared manifest lock. Calling
        # get_metadata() while holding this lock would deadlock on first use.
        current_for_missing_history = (
            self._metadata_cache
            if self._metadata_cache_loaded
            else await self.get_metadata(force=False)
        )

        async with self._lock:
            now = time.monotonic()
            if (
                not force
                and self._history_cache_fresh(now)
                and self._history_cache is not None
            ):
                return self._history_cache
            try:
                raw = await self._read_object(
                    self.settings.history_manifest_path,
                    max_bytes=self._HISTORY_MAX_BYTES,
                    too_large_code="audio_history_too_large",
                )
            except _ObjectNotFound:
                history = AudioHistory(
                    (current_for_missing_history,)
                    if current_for_missing_history is not None
                    else ()
                )
                self._history_cache = history
                self._history_cache_loaded = True
                self._history_cache_at = now
                return history

            try:
                value = json.loads(raw.decode("utf-8"))
                if not isinstance(value, dict):
                    raise ValueError("History manifest must be a JSON object.")
                history = AudioHistory.from_manifest(
                    value,
                    limit=self.settings.history_limit,
                )
            except (UnicodeDecodeError, ValueError) as exc:
                raise AudioStoreError(
                    f"Invalid audio history manifest: {exc}",
                    code="audio_history_invalid",
                ) from exc

            self._history_cache = history
            self._history_cache_loaded = True
            self._history_cache_at = now
            return history

    @staticmethod
    def _metadata_bytes(metadata: AudioMetadata) -> bytes:
        return json.dumps(
            metadata.to_manifest(),
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")

    @staticmethod
    def _history_bytes(history: AudioHistory) -> bytes:
        return json.dumps(
            history.to_manifest(),
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")

    async def _write_history(self, history: AudioHistory) -> None:
        await self._write_object(
            self.settings.history_manifest_path,
            self._history_bytes(history),
            "application/json",
            upsert=True,
        )

    def _stream_plain_audio(
        self,
        metadata: AudioMetadata,
        *,
        byte_range: tuple[int, int] | None,
    ) -> AsyncIterator[bytes]:
        async def generator() -> AsyncIterator[bytes]:
            hasher = hashlib.sha256()
            total = 0
            emitted = 0
            start, end = byte_range or (0, metadata.byte_length - 1)
            async for chunk in self._iter_object(
                metadata.object_path,
                max_bytes=self.settings.max_bytes,
                too_large_code="stored_audio_too_large",
            ):
                chunk_start = total
                chunk_end = total + len(chunk) - 1
                total += len(chunk)
                hasher.update(chunk)
                overlap_start = max(start, chunk_start)
                overlap_end = min(end, chunk_end)
                if overlap_start <= overlap_end:
                    output = chunk[
                        overlap_start - chunk_start : overlap_end - chunk_start + 1
                    ]
                    emitted += len(output)
                    if output:
                        yield output
            if total != metadata.byte_length:
                raise AudioStoreError(
                    "Stored audio byte length does not match its manifest.",
                    code="audio_size_mismatch",
                )
            if hasher.hexdigest() != metadata.sha256:
                raise AudioStoreError(
                    "Stored audio checksum does not match its manifest.",
                    code="audio_checksum_mismatch",
                )
            if emitted != end - start + 1:
                raise AudioStoreError(
                    "Stored audio range length is invalid.",
                    code="audio_range_size_mismatch",
                )

        return generator()

    def _stream_encrypted_audio(
        self,
        metadata: AudioMetadata,
        *,
        byte_range: tuple[int, int] | None,
    ) -> AsyncIterator[bytes]:
        async def generator() -> AsyncIterator[bytes]:
            if (
                not metadata.encryption_key_version
                or not metadata.ciphertext_sha256
                or not metadata.ciphertext_byte_length
            ):
                raise AudioStoreError(
                    "Encrypted audio metadata is incomplete.",
                    code="audio_manifest_invalid",
                )
            try:
                async for chunk in decrypt_audio_stream(
                    self._iter_object(
                        metadata.object_path,
                        max_bytes=self.settings.max_ciphertext_bytes,
                        too_large_code="stored_encrypted_audio_too_large",
                    ),
                    keys=self.settings.encryption_keys,
                    expected_plaintext_length=metadata.byte_length,
                    expected_plaintext_sha256=metadata.sha256,
                    expected_ciphertext_length=metadata.ciphertext_byte_length,
                    expected_ciphertext_sha256=metadata.ciphertext_sha256,
                    expected_key_version=metadata.encryption_key_version,
                    max_plaintext_bytes=self.settings.max_bytes,
                    byte_range=byte_range,
                ):
                    for offset in range(0, len(chunk), self.settings.response_chunk_bytes):
                        yield chunk[offset : offset + self.settings.response_chunk_bytes]
            except AudioEncryptionError as exc:
                raise AudioStoreError(str(exc), code=exc.code) from exc

        return generator()

    async def stream_audio(
        self,
        *,
        requested_version: str | None = None,
        byte_range: tuple[int, int] | None = None,
    ) -> tuple[AudioMetadata, AsyncIterator[bytes]]:
        metadata = await self.get_metadata(force=bool(requested_version))
        if metadata is None:
            raise _ObjectNotFound("No active audio is configured.")
        if requested_version and requested_version != metadata.version:
            raise AudioVersionChangedError(metadata.version)
        if byte_range is not None:
            start, end = byte_range
            if not 0 <= start <= end < metadata.byte_length:
                raise AudioStoreError(
                    "Requested audio byte range is invalid.",
                    code="audio_range_invalid",
                )
        stream = (
            self._stream_encrypted_audio(metadata, byte_range=byte_range)
            if metadata.encrypted
            else self._stream_plain_audio(metadata, byte_range=byte_range)
        )
        return metadata, stream

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
            stream = (
                self._stream_encrypted_audio(metadata, byte_range=None)
                if metadata.encrypted
                else self._stream_plain_audio(metadata, byte_range=None)
            )
            chunks = [chunk async for chunk in stream]
            data = b"".join(chunks)
            if len(data) > self.settings.max_bytes:
                raise AudioStoreError(
                    "Decrypted audio exceeded the configured maximum.",
                    code="decrypted_audio_too_large",
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

        detected_mime, safe_name = validate_audio_bytes(data, mime_type, file_name)
        plaintext_sha256 = hashlib.sha256(data).hexdigest()

        async with self._replace_lock:
            current = await self.get_metadata(force=True)
            if (
                telegram_update_id is not None
                and current is not None
                and current.telegram_update_id == telegram_update_id
            ):
                return current

            old_history = await self.get_history(force=True)
            now = datetime.now(UTC)
            version = f"{now.strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(6)}"

            if self.settings.encryption_enabled:
                key = self.settings.active_encryption_key
                if key is None:
                    raise AudioNotConfiguredError(
                        "The active audio encryption key is unavailable."
                    )
                try:
                    encrypted = encrypt_audio(
                        data,
                        key=key,
                        key_version=self.settings.encryption_active_key_version,
                        chunk_size=self.settings.encryption_chunk_bytes,
                        plaintext_sha256=plaintext_sha256,
                    )
                except AudioEncryptionError as exc:
                    raise AudioStoreError(str(exc), code=exc.code) from exc
                if encrypted.ciphertext_byte_length > self.settings.max_ciphertext_bytes:
                    raise AudioStoreError(
                        "Encrypted audio exceeds the configured storage maximum.",
                        code="encrypted_audio_too_large",
                    )
                object_path = f"versions/{version}/{safe_name}.agcm"
                stored_data = encrypted.data
                metadata = AudioMetadata(
                    version=version,
                    file_name=safe_name,
                    mime_type=detected_mime,
                    byte_length=len(data),
                    sha256=plaintext_sha256,
                    updated_at=now.isoformat(),
                    object_path=object_path,
                    uploaded_by=uploaded_by,
                    telegram_file_id=telegram_file_id,
                    telegram_update_id=telegram_update_id,
                    encrypted=True,
                    encryption_algorithm=self.settings.encryption_algorithm,
                    encryption_key_version=self.settings.encryption_active_key_version,
                    encryption_chunk_bytes=self.settings.encryption_chunk_bytes,
                    ciphertext_byte_length=encrypted.ciphertext_byte_length,
                    ciphertext_sha256=encrypted.ciphertext_sha256,
                )
                stored_content_type = "application/octet-stream"
            else:
                object_path = f"versions/{version}/{safe_name}"
                stored_data = data
                metadata = AudioMetadata(
                    version=version,
                    file_name=safe_name,
                    mime_type=detected_mime,
                    byte_length=len(data),
                    sha256=plaintext_sha256,
                    updated_at=now.isoformat(),
                    object_path=object_path,
                    uploaded_by=uploaded_by,
                    telegram_file_id=telegram_file_id,
                    telegram_update_id=telegram_update_id,
                )
                stored_content_type = detected_mime

            previous_items: list[AudioMetadata] = []
            if current is not None:
                previous_items.append(current)
            previous_items.extend(old_history.items)
            deduplicated: list[AudioMetadata] = [metadata]
            seen = {metadata.version}
            for item in previous_items:
                if item.version not in seen:
                    seen.add(item.version)
                    deduplicated.append(item)
            new_history = AudioHistory(
                tuple(deduplicated[: self.settings.history_limit])
            )

            async with self._lock:
                try:
                    await self._write_object(
                        object_path,
                        stored_data,
                        stored_content_type,
                        upsert=False,
                    )
                except Exception:
                    # A timed-out remote upload can still leave a complete or
                    # partial object. Remove the randomized version path before
                    # reporting failure; plaintext has never been written.
                    await self._cleanup_orphan(object_path)
                    raise
                history_written = False
                try:
                    await self._write_history(new_history)
                    history_written = True
                    await self._write_object(
                        self.settings.storage_manifest_path,
                        self._metadata_bytes(metadata),
                        "application/json",
                        upsert=True,
                    )
                except Exception:
                    if history_written:
                        try:
                            await self._write_history(old_history)
                        except AudioStoreError as restore_exc:
                            logger.error(
                                "Failed to restore audio history code=%s",
                                restore_exc.code,
                            )
                    await self._cleanup_orphan(object_path)
                    raise

                self._metadata_cache = metadata
                self._metadata_cache_loaded = True
                self._metadata_cache_at = time.monotonic()
                self._history_cache = new_history
                self._history_cache_loaded = True
                self._history_cache_at = time.monotonic()
                self._audio_cache_version = metadata.version
                self._audio_cache_bytes = data

            await self._cleanup_pruned_history(old_history, new_history)

            logger.info(
                "Website audio replaced version=%s plaintext_bytes=%s ciphertext_bytes=%s "
                "encrypted=%s key_version=%s uploaded_by=%s mode=%s",
                metadata.version,
                metadata.byte_length,
                metadata.ciphertext_byte_length or metadata.byte_length,
                metadata.encrypted,
                metadata.encryption_key_version,
                uploaded_by,
                self.mode,
            )
            return metadata

    async def rollback(self, selector: str) -> AudioMetadata:
        await self.ensure_ready()
        clean = selector.strip()
        if not clean:
            raise AudioHistorySelectionError("Specify a history number or version.")

        async with self._replace_lock:
            current = await self.get_metadata(force=True)
            history = await self.get_history(force=True)
            if not history.items:
                raise AudioHistorySelectionError("Audio history is empty.")

            target: AudioMetadata | None = None
            if clean.isdigit():
                index = int(clean)
                if 1 <= index <= len(history.items):
                    target = history.items[index - 1]
            else:
                target = next(
                    (item for item in history.items if item.version == clean),
                    None,
                )
            if target is None:
                raise AudioHistorySelectionError("Audio history selection was not found.")
            if current is not None and target.version == current.version:
                return current

            # Fully authenticate and validate the target before publishing it.
            _, validated = await self._get_audio_for_metadata(target)

            reordered = [target]
            reordered.extend(
                item for item in history.items if item.version != target.version
            )
            if current is not None and all(
                item.version != current.version for item in reordered
            ):
                reordered.append(current)
            new_history = AudioHistory(
                tuple(reordered[: self.settings.history_limit])
            )

            async with self._lock:
                await self._write_history(new_history)
                try:
                    await self._write_object(
                        self.settings.storage_manifest_path,
                        self._metadata_bytes(target),
                        "application/json",
                        upsert=True,
                    )
                except Exception:
                    try:
                        await self._write_history(history)
                    except AudioStoreError as restore_exc:
                        logger.error(
                            "Failed to restore audio history after rollback code=%s",
                            restore_exc.code,
                        )
                    raise

                self._metadata_cache = target
                self._metadata_cache_loaded = True
                self._metadata_cache_at = time.monotonic()
                self._history_cache = new_history
                self._history_cache_loaded = True
                self._history_cache_at = time.monotonic()
                self._audio_cache_version = target.version
                self._audio_cache_bytes = validated

            logger.info(
                "Website audio rolled back version=%s previous_version=%s",
                target.version,
                current.version if current else None,
            )
            return target

    async def _get_audio_for_metadata(
        self,
        metadata: AudioMetadata,
    ) -> tuple[AudioMetadata, bytes]:
        stream = (
            self._stream_encrypted_audio(metadata, byte_range=None)
            if metadata.encrypted
            else self._stream_plain_audio(metadata, byte_range=None)
        )
        chunks = [chunk async for chunk in stream]
        data = b"".join(chunks)
        if len(data) != metadata.byte_length:
            raise AudioStoreError(
                "Validated audio byte length is invalid.",
                code="decrypted_audio_size_mismatch",
            )
        return metadata, data


ObjectNotFound = _ObjectNotFound
