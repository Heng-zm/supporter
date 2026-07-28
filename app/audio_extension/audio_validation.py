from __future__ import annotations

from pathlib import PurePath
import re


MIME_ALIASES = {
    "audio/mp3": "audio/mpeg",
    "audio/x-mp3": "audio/mpeg",
    "audio/x-wav": "audio/wav",
    "audio/wave": "audio/wav",
    "audio/x-m4a": "audio/mp4",
    "audio/m4a": "audio/mp4",
    "audio/x-aac": "audio/aac",
    "audio/x-flac": "audio/flac",
}

MIME_EXTENSIONS = {
    "audio/mpeg": ".mp3",
    "audio/wav": ".wav",
    "audio/ogg": ".ogg",
    "audio/mp4": ".m4a",
    "audio/aac": ".aac",
    "audio/webm": ".webm",
    "audio/flac": ".flac",
}

EXTENSION_MIMES = {
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".m4a": "audio/mp4",
    ".mp4": "audio/mp4",
    ".aac": "audio/aac",
    ".webm": "audio/webm",
    ".flac": "audio/flac",
}


def normalize_mime_type(value: str | None) -> str:
    mime = str(value or "").split(";", 1)[0].strip().lower()
    return MIME_ALIASES.get(mime, mime)


def mime_from_file_name(file_name: str | None) -> str:
    extension = PurePath(str(file_name or "")).suffix.lower()
    return EXTENSION_MIMES.get(extension, "")


def sanitize_file_name(file_name: str | None, mime_type: str) -> str:
    raw = PurePath(str(file_name or "")).name.strip()
    raw = re.sub(r"[^A-Za-z0-9._ -]+", "_", raw)
    raw = re.sub(r"\s+", " ", raw).strip(" .")
    if not raw:
        raw = f"audio{MIME_EXTENSIONS.get(mime_type, '.bin')}"
    if len(raw) > 180:
        suffix = PurePath(raw).suffix[:16]
        raw = f"{PurePath(raw).stem[:160]}{suffix}"
    if not PurePath(raw).suffix and mime_type in MIME_EXTENSIONS:
        raw = f"{raw}{MIME_EXTENSIONS[mime_type]}"
    return raw


def detect_audio_mime(data: bytes) -> str:
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "audio/wav"
    if data.startswith(b"OggS"):
        return "audio/ogg"
    if data.startswith(b"fLaC"):
        return "audio/flac"
    if data.startswith(b"\x1aE\xdf\xa3"):
        return "audio/webm"
    if len(data) >= 12 and data[4:8] == b"ftyp":
        return "audio/mp4"
    if data.startswith(b"ID3"):
        return "audio/mpeg"
    if len(data) >= 2 and data[0] == 0xFF:
        second = data[1]
        if second & 0xF6 == 0xF0:
            return "audio/aac"
        if second & 0xE0 == 0xE0:
            return "audio/mpeg"
    return ""


def validate_audio_bytes(
    data: bytes,
    declared_mime_type: str | None,
    file_name: str | None,
) -> tuple[str, str]:
    if not data:
        raise ValueError("The audio file is empty.")

    declared = normalize_mime_type(declared_mime_type)
    extension_mime = mime_from_file_name(file_name)
    detected = detect_audio_mime(data)

    if not detected:
        raise ValueError(
            "Unsupported or unrecognized audio binary. Use MP3, WAV, OGG, M4A, AAC, WebM, or FLAC."
        )

    if declared and declared not in MIME_EXTENSIONS:
        raise ValueError(f"Unsupported declared audio type: {declared}.")
    if declared and declared != detected:
        raise ValueError(
            f"Audio content type mismatch: declared {declared}, detected {detected}."
        )
    if extension_mime and extension_mime != detected:
        raise ValueError(
            f"Audio file extension mismatch: extension indicates {extension_mime}, detected {detected}."
        )

    file_name_clean = sanitize_file_name(file_name, detected)
    return detected, file_name_clean
