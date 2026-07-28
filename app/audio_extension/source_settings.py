from __future__ import annotations

from pathlib import Path

# Non-secret audio extension settings live in source code so they do not need
# separate Render/environment entries. Keep credentials, bot tokens, and
# encryption keys in the environment; never place secrets in this file.
AUDIO_ENABLED = True
AUDIO_STORAGE_MODE = "auto"
AUDIO_STORAGE_BUCKET = "website-audio"
AUDIO_STORAGE_MANIFEST_PATH = "current.json"
AUDIO_HISTORY_MANIFEST_PATH = "history.json"
AUDIO_MAX_BYTES = 20_000_000
AUDIO_METADATA_CACHE_SECONDS = 5
AUDIO_PENDING_TTL_SECONDS = 600
AUDIO_HTTP_TIMEOUT_SECONDS = 60
AUDIO_REQUIRE_PERSISTENT_STORAGE = True
AUDIO_AUTO_CREATE_BUCKET = True
AUDIO_TELEGRAM_ALLOW_OWNER_PRIVATE_CHAT = True

# Encryption-at-rest settings. AUDIO_ENCRYPTION_KEY_<VERSION> values remain in
# the environment. Example: AUDIO_ENCRYPTION_KEY_V1=<base64 32-byte key>.
AUDIO_ENCRYPTION_ENABLED = True
AUDIO_ENCRYPTION_ALGORITHM = "AES-256-GCM-CHUNKED"
AUDIO_ENCRYPTION_ACTIVE_KEY_VERSION = "v1"
AUDIO_ENCRYPTION_CHUNK_BYTES = 1_048_576
AUDIO_HISTORY_LIMIT = 10
AUDIO_RANGE_REQUESTS_ENABLED = True
AUDIO_RESPONSE_CHUNK_BYTES = 64 * 1024

# Local storage is intended only for explicit development use. With
# AUDIO_REQUIRE_PERSISTENT_STORAGE=True, production still requires Supabase.
AUDIO_LOCAL_STORAGE_DIRECTORY = Path("data/website-audio")

# Browser origins allowed to call the public supporter/audio API. CORS origin
# matching is exact, so values are stored without trailing slashes.
BACKEND_CORS_ORIGINS = (
    "https://pay-coffee-topaz.vercel.app",
    "https://j-s-ng-o-rgn-sz-lrgkldgs.vercel.app",
)
