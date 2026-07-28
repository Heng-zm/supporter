from __future__ import annotations

from dataclasses import dataclass
from typing import Any


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

    def to_manifest(self) -> dict[str, Any]:
        return {
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
        }

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "available": True,
            "version": self.version,
            "fileName": self.file_name,
            "mimeType": self.mime_type,
            "byteLength": self.byte_length,
            "sha256": self.sha256,
            "updatedAt": self.updated_at,
        }

    @classmethod
    def from_manifest(cls, value: dict[str, Any]) -> "AudioMetadata":
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

        if not version or len(version) > 160:
            raise ValueError("Audio manifest version is invalid.")
        if not file_name or len(file_name) > 255:
            raise ValueError("Audio manifest fileName is invalid.")
        if not mime_type or len(mime_type) > 100:
            raise ValueError("Audio manifest mimeType is invalid.")
        if byte_length <= 0:
            raise ValueError("Audio manifest byteLength is invalid.")
        if len(sha256) != 64 or any(ch not in "0123456789abcdef" for ch in sha256):
            raise ValueError("Audio manifest sha256 is invalid.")
        if not updated_at or len(updated_at) > 100:
            raise ValueError("Audio manifest updatedAt is invalid.")
        if not object_path or ".." in object_path.split("/"):
            raise ValueError("Audio manifest objectPath is invalid.")

        uploaded_by_raw = value.get("uploadedBy")
        uploaded_by = None
        if uploaded_by_raw is not None:
            try:
                uploaded_by = int(uploaded_by_raw)
            except (TypeError, ValueError) as exc:
                raise ValueError("Audio manifest uploadedBy is invalid.") from exc

        telegram_update_id_raw = value.get("telegramUpdateId")
        telegram_update_id = None
        if telegram_update_id_raw is not None:
            try:
                telegram_update_id = int(telegram_update_id_raw)
            except (TypeError, ValueError) as exc:
                raise ValueError("Audio manifest telegramUpdateId is invalid.") from exc

        telegram_file_id_raw = value.get("telegramFileId")
        telegram_file_id = (
            str(telegram_file_id_raw).strip() if telegram_file_id_raw else None
        )

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
        )
