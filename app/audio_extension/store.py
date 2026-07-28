from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import hashlib
import json
import logging
from pathlib import Path
import secrets
import time
from urllib.parse import quote

import httpx

from .audio_validation import validate_audio_bytes
from .config import AudioSettings
from .models import AudioMetadata

logger = logging.getLogger(__name__)


class AudioStoreError(RuntimeError):
    pass


class AudioNotConfiguredError(AudioStoreError):
    pass


class AudioVersionChangedError(AudioStoreError):
    pass


class _ObjectNotFound(AudioStoreError):
    pass


class AudioStore:
    def __init__(
        self,
        settings: AudioSettings,
        client: httpx.AsyncClient,
    ) -> None:
        self.settings = settings
        self.client = client
        self._lock = asyncio.Lock()
        self._metadata_cache: AudioMetadata | None = None
        self._metadata_cache_at = 0.0
        self._audio_cache_version = ""
        self._audio_cache_bytes: bytes | None = None

    @property
    def mode(self) -> str:
        return self.settings.resolved_storage_mode

    @property
    def available(self) -> bool:
        return not self.settings.configuration_error

    def _require_available(self) -> None:
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

    def _local_path(self, object_path: str) -> Path:
        clean_parts = [part for part in object_path.split("/") if part]
        if not clean_parts or any(part in {".", ".."} for part in clean_parts):
            raise AudioStoreError("Invalid local audio object path.")
        return self.settings.local_storage_directory.joinpath(*clean_parts)

    async def _read_object(self, object_path: str) -> bytes:
        if self.mode == "supabase":
            response = await self.client.get(
                self._supabase_object_url(object_path),
                headers=self._supabase_headers(),
            )
            if response.status_code == 404:
                raise _ObjectNotFound(object_path)
            if response.status_code >= 400:
                raise AudioStoreError(
                    f"Supabase Storage read failed with HTTP {response.status_code}."
                )
            return bytes(response.content)

        path = self._local_path(object_path)
        try:
            return await asyncio.to_thread(path.read_bytes)
        except FileNotFoundError as exc:
            raise _ObjectNotFound(object_path) from exc
        except OSError as exc:
            raise AudioStoreError(f"Local audio read failed: {exc}") from exc

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
            response = await self.client.post(
                self._supabase_object_url(object_path),
                headers=headers,
                content=data,
            )
            if response.status_code >= 400:
                detail = response.text[:500]
                raise AudioStoreError(
                    f"Supabase Storage upload failed with HTTP {response.status_code}: {detail}"
                )
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
            raise AudioStoreError(f"Local audio write failed: {exc}") from exc

    async def get_metadata(self, *, force: bool = False) -> AudioMetadata | None:
        self._require_available()
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
                raise AudioStoreError(f"Invalid audio manifest: {exc}") from exc

            if metadata.byte_length > self.settings.max_bytes:
                raise AudioStoreError("Stored audio exceeds AUDIO_MAX_BYTES.")

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
                    "Stored audio byte length does not match its manifest."
                )
            actual_hash = hashlib.sha256(data).hexdigest()
            if actual_hash != metadata.sha256:
                raise AudioStoreError("Stored audio checksum does not match its manifest.")

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
        self._require_available()
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
