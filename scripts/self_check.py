from __future__ import annotations

import asyncio
from pathlib import Path
import tempfile
import wave
import io
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import httpx

from app.audio_extension.config import AudioSettings
from app.audio_extension.store import AudioStore


def sample_wav() -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(8_000)
        wav.writeframes(b"\x00\x00" * 800)
    return output.getvalue()


async def main() -> None:
    with tempfile.TemporaryDirectory() as directory:
        settings = AudioSettings(
            enabled=True,
            storage_mode="local",
            storage_bucket="website-audio",
            storage_manifest_path="current.json",
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
        )
        async with httpx.AsyncClient() as client:
            store = AudioStore(settings, client)
            source = sample_wav()
            metadata = await store.replace(
                file_name="self-check.wav",
                mime_type="audio/wav",
                data=source,
                uploaded_by=1,
                telegram_file_id=None,
                telegram_update_id=None,
            )
            loaded_metadata, loaded = await store.get_audio(
                requested_version=metadata.version
            )
            assert loaded == source
            assert loaded_metadata.sha256 == metadata.sha256
            print("Audio extension self-check passed.")
            print(f"Version: {metadata.version}")
            print(f"Bytes: {metadata.byte_length}")
            print(f"SHA-256: {metadata.sha256}")


if __name__ == "__main__":
    asyncio.run(main())
