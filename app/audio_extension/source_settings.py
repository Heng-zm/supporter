from __future__ import annotations

from pathlib import Path

# Non-secret audio extension settings live in source code so they do not need
# separate Render/environment entries. Keep credentials and tokens in the
# environment; never place secrets in this file.
AUDIO_ENABLED = True
AUDIO_STORAGE_MODE = "auto"
AUDIO_STORAGE_BUCKET = "website-audio"
AUDIO_STORAGE_MANIFEST_PATH = "current.json"
AUDIO_MAX_BYTES = 20_000_000
AUDIO_METADATA_CACHE_SECONDS = 5
AUDIO_PENDING_TTL_SECONDS = 600
AUDIO_HTTP_TIMEOUT_SECONDS = 60
AUDIO_REQUIRE_PERSISTENT_STORAGE = True
AUDIO_TELEGRAM_ALLOW_OWNER_PRIVATE_CHAT = True

# Local storage is intended only for explicit development use. With
# AUDIO_REQUIRE_PERSISTENT_STORAGE=True, production still requires Supabase.
AUDIO_LOCAL_STORAGE_DIRECTORY = Path("data/website-audio")
