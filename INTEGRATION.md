# Add `/audio` to the existing supporter FastAPI backend

This patch is additive. It does not replace supporter, visit, Supabase, webhook-security, replay-protection, or existing Telegram management code.

## Production 404 fix

The package now includes a drop-in `app/main.py` for the current supporter backend structure. The deployed application previously registered only the supporter and visit routers, so FastAPI returned 404 before any audio storage code could run.

After copying the files, run:

```bash
python scripts/check_audio_routes.py
```

Do not deploy until it reports both `/api/audio/metadata` and `/api/audio/file`. Also confirm Render starts `uvicorn app.main:app`; changing a different module will not affect the live service.

## 1. Copy the extension

Copy `app/audio_extension` into the existing backend so these modules are inside the existing `app` package.

## 2. Register the public FastAPI routes and service

Add imports in the FastAPI application module:

```python
from app.audio_extension import (
    close_audio_extension,
    include_audio_router,
    start_audio_extension,
)
```

Register the routes once, next to the current supporter and visit routers:

```python
include_audio_router(app, api_prefix=runtime_settings.api_prefix)
```

Inside the existing lifespan, start the extension with its dedicated upload/download client. This avoids inheriting a shorter timeout from supporter-list requests:

```python
await start_audio_extension(app)
try:
    yield
finally:
    await close_audio_extension(app)
```

The dedicated timeout is controlled in `app/audio_extension/source_settings.py` by `AUDIO_HTTP_TIMEOUT_SECONDS = 60`. Passing an existing `httpx.AsyncClient` is supported only when that client has suitable file-transfer timeouts.

This adds:

```text
GET /api/audio/metadata
GET /api/audio/file?version=...
```

## 3A. Recommended for a python-telegram-bot `Application`

The current webhook can continue converting JSON with `Update.de_json(...)` and calling `application.process_update(update)`. Register the audio handler in that same application instead of modifying the webhook body.

After the Telegram `Application` is created, add:

```python
from app.audio_extension import register_python_telegram_bot_handler

register_python_telegram_bot_handler(
    telegram_application,
    app.state.audio_telegram,
    group=-90,
)
```

The handler stops further dispatch only when it actually handles `/audio` or a pending audio upload. Other supporter and bot messages continue to the existing handlers.

## 3B. Alternative for a raw-JSON Telegram webhook

For a backend that dispatches the webhook dictionary directly, import:

```python
from app.audio_extension import handle_audio_telegram_update
```

After the existing webhook secret, source, rate-limit, replay, chat, and admin checks, but before the supporter command dispatcher, add:

```python
if await handle_audio_telegram_update(request.app, update_json):
    return {"ok": True}
```

Keep all current webhook security and replay protection unchanged.

## 4. CORS

The two deployed frontend origins are stored in `app/audio_extension/source_settings.py`:

```python
BACKEND_CORS_ORIGINS = (
    "https://pay-coffee-topaz.vercel.app",
    "https://j-s-ng-o-rgn-sz-lrgkldgs.vercel.app",
)
```

Do not add trailing slashes. Import the validated source-controlled list into the existing FastAPI CORS middleware:

```python
from app.audio_extension import get_backend_cors_origins

app.add_middleware(
    CORSMiddleware,
    allow_origins=list(get_backend_cors_origins()),
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Accept", "Content-Type", "X-Telegram-Bot-Api-Secret-Token"],
    expose_headers=[
        "X-Supporters-Source",
        "Warning",
        "ETag",
        "X-Audio-Version",
    ],
)
```

No `BACKEND_CORS_ORIGINS` Render environment variable is required. The React app needs public `GET` access only; `POST` remains available for the Telegram webhook.

## 5. Supabase Storage

Run `supabase_audio_storage.sql` once. Keep the bucket private. The server accepts either `SUPABASE_SECRET_KEY` or `SUPABASE_SERVICE_ROLE_KEY` and never exposes it to React.

The upload sequence is:

1. Write an immutable versioned binary object.
2. Write `current.json` only after the binary succeeds.
3. React sees the new version and downloads that exact version.

## 6. Render environment

Add only credentials, identifiers, and secrets from `.env.audio.example`:

```env
APP_ENVIRONMENT=production
SUPABASE_URL=https://YOUR_PROJECT.supabase.co
SUPABASE_SECRET_KEY=YOUR_SERVICE_ROLE_OR_SERVER_SECRET
TELEGRAM_BOT_TOKEN=123456:replace-me
TELEGRAM_CHAT_ID=123456789
TELEGRAM_ADMIN_USER_IDS=123456789
TELEGRAM_WEBHOOK_SECRET=GENERATE_A_DISTINCT_SECRET
```

The non-secret audio configuration is stored in:

```text
app/audio_extension/source_settings.py
```

No `AUDIO_*` Render environment entries are required. With the included source settings, `auto` resolves to private Supabase Storage and persistent storage is required. The local directory is retained for controlled development changes, but Render local storage is ephemeral and is not accepted while `AUDIO_REQUIRE_PERSISTENT_STORAGE = True`.

## 7. Telegram usage

```text
/audio
```

Then send an MP3, WAV, OGG, M4A, AAC, WebM, or FLAC file.

Other forms:

```text
/audio status
/audio cancel
```

You can also send an audio file with caption `/audio`, or reply `/audio` to an existing audio message. The default maximum is 20,000,000 bytes.

## 8. Frontend

Deploy the included frontend with:

```env
VITE_BACKEND_URL=https://your-supporter-backend.onrender.com
```

The solid-white page checks every 15 seconds, validates byte length and SHA-256, and keeps the previous track if the replacement cannot be loaded.
