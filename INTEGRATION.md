# Add encrypted `/audio` management to the supporter backend

This patch is additive. It preserves supporter, visit, Supabase, Telegram webhook security, replay protection, and existing management code.

## 1. Install dependencies

```bash
pip install -r requirements-audio.txt
```

The encryption extension requires `cryptography` for AES-256-GCM.

## 2. Copy backend files

Copy `app/audio_extension` into the existing backend and apply the included `app/main.py` integration. Render must start:

```bash
uvicorn app.main:app --host 0.0.0.0 --port $PORT
```

Verify routes before deployment:

```bash
python scripts/check_audio_routes.py
```

Expected routes:

```text
/api/v1/audio/metadata
/api/v1/audio/file
/api/telegram/webhook
```

## 3. Configure Supabase

Run `supabase_audio_storage.sql` in the Supabase SQL Editor. Rerun it when upgrading from v2.2 or earlier so the bucket accepts encrypted `application/octet-stream` objects and the larger authenticated-container size.

The bucket remains private. Use only a backend secret or service-role key.

## 4. Generate the encryption key

```bash
python scripts/generate_audio_key.py --version v1
```

Add the output to Render along with the other secrets from `.env.audio.example`:

```env
SUPABASE_URL=https://YOUR_PROJECT.supabase.co
SUPABASE_SECRET_KEY=YOUR_SERVER_SECRET_OR_SERVICE_ROLE_KEY
AUDIO_ENCRYPTION_KEY_V1=BASE64_ENCODED_32_BYTE_KEY

TELEGRAM_BOT_TOKEN=YOUR_BOT_TOKEN
TELEGRAM_CHAT_ID=YOUR_CHAT_ID
TELEGRAM_ADMIN_USER_IDS=YOUR_NUMERIC_USER_ID
TELEGRAM_WEBHOOK_SECRET=YOUR_RANDOM_URL_SAFE_SECRET
TELEGRAM_WEBHOOK_URL=https://supporter-ipio.onrender.com/api/telegram/webhook
TELEGRAM_AUTO_CONFIGURE_WEBHOOK=true
```

All non-secret audio behavior remains in `app/audio_extension/source_settings.py`.

## 5. CORS

Browser range requests require `Range` in allowed headers and the following exposed headers:

```python
allow_headers=[
    "Accept",
    "Content-Type",
    "If-None-Match",
    "If-Range",
    "Range",
    "X-Telegram-Bot-Api-Secret-Token",
]
expose_headers=[
    "ETag",
    "X-Audio-Version",
    "Content-Length",
    "Content-Range",
    "Accept-Ranges",
]
```

The included `app/main.py` already contains these values and both source-controlled Vercel origins.

## 6. Telegram commands

```text
/audio                    wait for the next audio upload
/audio status             show the active version and encryption key version
/audio history            list retained encrypted versions
/audio rollback 2         restore the previous version
/audio rollback <version> restore an exact version
/audio cancel             cancel a pending upload
```

Audio can also be sent with caption `/audio`, or `/audio` can reply to an existing audio message.

## 7. Storage publication order

1. Validate plaintext bytes and calculate plaintext SHA-256.
2. Encrypt in memory with a new per-file nonce seed.
3. Calculate ciphertext SHA-256.
4. Upload the immutable encrypted version.
5. Write `history.json`.
6. Write `current.json` last.

If upload or manifest publication fails, the randomized encrypted object is removed on a best-effort basis and the previous active manifest remains unchanged.

## 8. API behavior

```text
GET /api/v1/audio/metadata
GET /api/v1/audio/file?version=<active-version>
```

`/api/v1/audio/file` streams authenticated plaintext from encrypted storage. It supports a single HTTP byte range and returns `206` when valid. The key and ciphertext are never sent to React. The legacy `/api/audio/*` aliases remain available during migration.

## 9. Key rotation

Add the new key while retaining old keys, then change `AUDIO_ENCRYPTION_ACTIVE_KEY_VERSION` in source. See `ENCRYPTION.md` for the exact sequence.

## 10. Verification

```bash
pytest -q
python scripts/self_check.py
python scripts/check_audio_routes.py
```

After Render deployment, check `/health`:

```text
audioStorageReady: true
audioEncryptionEnabled: true
audioEncryptionActiveKeyVersion: v1
audioTelegramWebhookConfigured: true
```
