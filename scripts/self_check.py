from __future__ import annotations

import asyncio
import hashlib
import io
from pathlib import Path
import tempfile
import wave
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import httpx

from app.audio_extension.config import AudioSettings
from app.audio_extension.store import AudioStore


def sample_wav(marker: bytes = b"\x00\x00") -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(8_000)
        wav.writeframes(marker * 800)
    return output.getvalue()


async def main() -> None:
    with tempfile.TemporaryDirectory() as directory:
        settings = AudioSettings(
            enabled=True,
            storage_mode="local",
            storage_bucket="website-audio",
            storage_manifest_path="current.json",
            history_manifest_path="history.json",
            local_storage_directory=Path(directory),
            max_bytes=20_000_000,
            metadata_cache_seconds=0,
            pending_ttl_seconds=600,
            http_timeout_seconds=60,
            supabase_url="",
            supabase_secret_key="",
            telegram_bot_token="",
            telegram_chat_id="",
            telegram_admin_user_ids=frozenset(),
            telegram_allow_owner_private_chat=True,
            app_environment="test",
            require_persistent_storage=False,
            encryption_enabled=True,
            encryption_algorithm="AES-256-GCM-CHUNKED",
            encryption_active_key_version="v1",
            encryption_chunk_bytes=64 * 1024,
            encryption_keys={"v1": b"S" * 32},
            history_limit=10,
            range_requests_enabled=True,
            response_chunk_bytes=64 * 1024,
        )
        async with httpx.AsyncClient() as client:
            store = AudioStore(settings, client)
            first_source = sample_wav() + b"A" * 80_000
            first = await store.replace(
                file_name="first.wav",
                mime_type="audio/wav",
                data=first_source,
                uploaded_by=1,
                telegram_file_id=None,
                telegram_update_id=1,
            )
            encrypted = (Path(directory) / first.object_path).read_bytes()
            assert encrypted.startswith(b"RAAEGCM1")
            assert not encrypted.startswith(b"RIFF")
            assert hashlib.sha256(encrypted).hexdigest() == first.ciphertext_sha256

            second_source = sample_wav(b"\x01\x00") + b"B" * 90_000
            second = await store.replace(
                file_name="second.wav",
                mime_type="audio/wav",
                data=second_source,
                uploaded_by=1,
                telegram_file_id=None,
                telegram_update_id=2,
            )
            assert second.version != first.version

            rolled_back = await store.rollback("2")
            assert rolled_back.version == first.version
            reader = AudioStore(settings, client)
            loaded_metadata, loaded = await reader.get_audio(
                requested_version=first.version
            )
            assert loaded == first_source
            assert loaded_metadata.sha256 == hashlib.sha256(first_source).hexdigest()

            _, range_stream = await reader.stream_audio(
                requested_version=first.version,
                byte_range=(100, 999),
            )
            ranged = b"".join([chunk async for chunk in range_stream])
            assert ranged == first_source[100:1000]

            print("Audio encryption self-check passed.")
            print(f"Version: {first.version}")
            print(f"Plaintext bytes: {first.byte_length}")
            print(f"Ciphertext bytes: {first.ciphertext_byte_length}")
            print(f"Plaintext SHA-256: {first.sha256}")
            print(f"Ciphertext SHA-256: {first.ciphertext_sha256}")
            print(f"Key version: {first.encryption_key_version}")


if __name__ == "__main__":
    asyncio.run(main())
