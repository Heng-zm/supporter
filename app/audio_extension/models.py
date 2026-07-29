from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

_VERSION_RE = re.compile(r"^[A-Za-z0-9._-]{1,160}$")
_KEY_VERSION_RE = re.compile(r"^[A-Za-z0-9._-]{1,32}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ALLOWED_MIME_TYPES = {
    "audio/mpeg",
    "audio/wav",
    "audio/ogg",
    "audio/mp4",
    "audio/aac",
    "audio/webm",
    "audio/flac",
}
_ALLOWED_ENCRYPTION_ALGORITHMS = {"AES-256-GCM-CHUNKED"}


@dataclass(frozen=True, slots=True)
class AudioMetadata:
    version: str
    file_name: str
    mime_type: str
    byte_length: int
    sha256: str
    updated_at: str
    object_path: str
    uploaded_by: int | None = None
    telegram_file_id: str | None = None
    telegram_update_id: int | None = None
    encrypted: bool = False
    encryption_algorithm: str | None = None
    encryption_key_version: str | None = None
    encryption_chunk_bytes: int | None = None
    ciphertext_byte_length: int | None = None
    ciphertext_sha256: str | None = None

    def to_manifest(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "version": self.version,
            "fileName": self.file_name,
            "mimeType": self.mime_type,
            "byteLength": self.byte_length,
            "sha256": self.sha256,
            "updatedAt": self.updated_at,
            "objectPath": self.object_path,
            "uploadedBy": self.uploaded_by,
            "telegramFileId": self.telegram_file_id,
            "telegramUpdateId": self.telegram_update_id,
            "encrypted": self.encrypted,
        }
        if self.encrypted:
            value["encryption"] = {
                "algorithm": self.encryption_algorithm,
                "keyVersion": self.encryption_key_version,
                "chunkBytes": self.encryption_chunk_bytes,
                "ciphertextByteLength": self.ciphertext_byte_length,
                "ciphertextSha256": self.ciphertext_sha256,
            }
        return value

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "available": True,
            "version": self.version,
            "fileName": self.file_name,
            "mimeType": self.mime_type,
            "byteLength": self.byte_length,
            "sha256": self.sha256,
            "updatedAt": self.updated_at,
            "encryptedAtRest": self.encrypted,
        }

    @classmethod
    def from_manifest(cls, value: dict[str, Any]) -> AudioMetadata:
        version = str(value.get("version") or "").strip()
        file_name = str(value.get("fileName") or "").strip()
        mime_type = str(value.get("mimeType") or "").strip().lower()
        sha256 = str(value.get("sha256") or "").strip().lower()
        updated_at = str(value.get("updatedAt") or "").strip()
        object_path = str(value.get("objectPath") or "").strip().lstrip("/")

        try:
            byte_length = int(value.get("byteLength") or 0)
        except (TypeError, ValueError) as exc:
            raise ValueError("Audio manifest byteLength is invalid.") from exc

        if not _VERSION_RE.fullmatch(version):
            raise ValueError("Audio manifest version is invalid.")
        if not file_name or len(file_name) > 255 or any(ch in file_name for ch in "\r\n"):
            raise ValueError("Audio manifest fileName is invalid.")
        if mime_type not in _ALLOWED_MIME_TYPES:
            raise ValueError("Audio manifest mimeType is invalid.")
        if byte_length <= 0:
            raise ValueError("Audio manifest byteLength is invalid.")
        if not _SHA256_RE.fullmatch(sha256):
            raise ValueError("Audio manifest sha256 is invalid.")
        try:
            parsed_updated_at = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("Audio manifest updatedAt is invalid.") from exc
        if parsed_updated_at.tzinfo is None:
            raise ValueError("Audio manifest updatedAt must include a timezone.")

        parts = object_path.split("/")
        if (
            not object_path
            or len(object_path) > 1024
            or any(part in {"", ".", ".."} for part in parts)
            or not object_path.startswith("versions/")
        ):
            raise ValueError("Audio manifest objectPath is invalid.")

        uploaded_by_raw = value.get("uploadedBy")
        uploaded_by = None
        if uploaded_by_raw is not None:
            try:
                uploaded_by = int(uploaded_by_raw)
            except (TypeError, ValueError) as exc:
                raise ValueError("Audio manifest uploadedBy is invalid.") from exc
            if uploaded_by <= 0:
                raise ValueError("Audio manifest uploadedBy is invalid.")

        telegram_update_id_raw = value.get("telegramUpdateId")
        telegram_update_id = None
        if telegram_update_id_raw is not None:
            try:
                telegram_update_id = int(telegram_update_id_raw)
            except (TypeError, ValueError) as exc:
                raise ValueError("Audio manifest telegramUpdateId is invalid.") from exc
            if telegram_update_id < 0:
                raise ValueError("Audio manifest telegramUpdateId is invalid.")

        telegram_file_id_raw = value.get("telegramFileId")
        telegram_file_id = (
            str(telegram_file_id_raw).strip() if telegram_file_id_raw else None
        )
        if telegram_file_id is not None and len(telegram_file_id) > 512:
            raise ValueError("Audio manifest telegramFileId is invalid.")

        encrypted = bool(value.get("encrypted", False))
        encryption_algorithm: str | None = None
        encryption_key_version: str | None = None
        encryption_chunk_bytes: int | None = None
        ciphertext_byte_length: int | None = None
        ciphertext_sha256: str | None = None

        if encrypted:
            encryption = value.get("encryption")
            if not isinstance(encryption, dict):
                raise ValueError("Audio manifest encryption block is invalid.")
            encryption_algorithm = str(encryption.get("algorithm") or "").strip()
            encryption_key_version = str(
                encryption.get("keyVersion") or ""
            ).strip().lower()
            ciphertext_sha256 = str(
                encryption.get("ciphertextSha256") or ""
            ).strip().lower()
            try:
                encryption_chunk_bytes = int(encryption.get("chunkBytes") or 0)
                ciphertext_byte_length = int(
                    encryption.get("ciphertextByteLength") or 0
                )
            except (TypeError, ValueError) as exc:
                raise ValueError("Audio manifest encryption sizes are invalid.") from exc

            if encryption_algorithm not in _ALLOWED_ENCRYPTION_ALGORITHMS:
                raise ValueError("Audio manifest encryption algorithm is invalid.")
            if not _KEY_VERSION_RE.fullmatch(encryption_key_version):
                raise ValueError("Audio manifest encryption keyVersion is invalid.")
            if not 64 * 1024 <= encryption_chunk_bytes <= 4 * 1024 * 1024:
                raise ValueError("Audio manifest encryption chunkBytes is invalid.")
            if ciphertext_byte_length <= byte_length:
                raise ValueError(
                    "Audio manifest ciphertextByteLength must exceed byteLength."
                )
            if not _SHA256_RE.fullmatch(ciphertext_sha256):
                raise ValueError("Audio manifest ciphertextSha256 is invalid.")

        return cls(
            version=version,
            file_name=file_name,
            mime_type=mime_type,
            byte_length=byte_length,
            sha256=sha256,
            updated_at=updated_at,
            object_path=object_path,
            uploaded_by=uploaded_by,
            telegram_file_id=telegram_file_id,
            telegram_update_id=telegram_update_id,
            encrypted=encrypted,
            encryption_algorithm=encryption_algorithm,
            encryption_key_version=encryption_key_version,
            encryption_chunk_bytes=encryption_chunk_bytes,
            ciphertext_byte_length=ciphertext_byte_length,
            ciphertext_sha256=ciphertext_sha256,
        )


@dataclass(frozen=True, slots=True)
class AudioHistory:
    items: tuple[AudioMetadata, ...]

    def to_manifest(self) -> dict[str, Any]:
        return {
            "formatVersion": 1,
            "items": [item.to_manifest() for item in self.items],
        }

    @classmethod
    def from_manifest(cls, value: dict[str, Any], *, limit: int) -> AudioHistory:
        if value.get("formatVersion") != 1:
            raise ValueError("Audio history formatVersion is invalid.")
        raw_items = value.get("items")
        if not isinstance(raw_items, list):
            raise ValueError("Audio history items are invalid.")
        if len(raw_items) > max(limit, 100):
            raise ValueError("Audio history contains too many entries.")
        items: list[AudioMetadata] = []
        seen: set[str] = set()
        for raw in raw_items:
            if not isinstance(raw, dict):
                raise ValueError("Audio history item is invalid.")
            item = AudioMetadata.from_manifest(raw)
            if item.version in seen:
                raise ValueError("Audio history contains duplicate versions.")
            seen.add(item.version)
            items.append(item)
        return cls(tuple(items[:limit]))
