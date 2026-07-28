# Audio encryption at rest — v2.3.0

## Design

New Telegram uploads are validated in memory, encrypted before storage, and saved as an authenticated chunk container:

```text
Telegram bytes
  → audio signature and size validation
  → plaintext SHA-256
  → AES-256-GCM authenticated chunks
  → ciphertext SHA-256
  → private Supabase object: versions/<version>/<name>.agcm
  → history.json
  → current.json published last
```

The backend never writes plaintext audio to a temporary file. Local development storage also receives only the encrypted `.agcm` object.

Each file uses a random 16-byte nonce seed. Every chunk uses a distinct 96-bit nonce formed from that random seed plus the chunk index through SHA-256. The authenticated additional data includes the complete container header, chunk index, and chunk length.

Independent GCM tags are used per chunk. The API authenticates each chunk before yielding its plaintext, which supports safe streaming and single HTTP byte ranges. A request for a range still validates the entire encrypted object, plaintext length, plaintext SHA-256, ciphertext length, and ciphertext SHA-256 before the stream completes.

## Required backend secret

Generate the first key:

```bash
python scripts/generate_audio_key.py --version v1
```

Add the printed value to Render:

```env
AUDIO_ENCRYPTION_KEY_V1=<base64 32-byte key>
```

Never put this key in React, Vercel, Git, Supabase tables, Telegram messages, logs, or chat.

## Key rotation

1. Generate the next key:

   ```bash
   python scripts/generate_audio_key.py --version v2
   ```

2. Add `AUDIO_ENCRYPTION_KEY_V2` to Render while retaining `AUDIO_ENCRYPTION_KEY_V1`.
3. Change this source setting:

   ```python
   AUDIO_ENCRYPTION_ACTIVE_KEY_VERSION = "v2"
   ```

4. Redeploy.

New uploads use `v2`; existing `v1` objects and rollback versions remain readable because both keys are present. Do not remove an old key until every retained history entry using it has been permanently removed.

## Telegram administration

```text
/audio status
/audio history
/audio rollback 2
/audio rollback <exact-version>
```

History is newest first. Entry `1` is normally the active version, so `/audio rollback 2` activates the immediately previous version.

## Storage objects

```text
versions/<version>/<original-name>.agcm  encrypted audio
current.json                             active metadata
history.json                             retained rollback metadata
```

Metadata contains no encryption key. It stores the algorithm, key version, plaintext length/hash, and ciphertext length/hash needed for validation.

## Range behavior

The public endpoint accepts one byte range:

```http
Range: bytes=1000-9999
```

Valid requests return `206 Partial Content`, `Content-Range`, and `Accept-Ranges: bytes`. Multiple ranges are rejected with `416` because multipart ranges are intentionally unsupported.

## Migration from earlier versions

Existing unencrypted manifests remain readable as legacy plaintext until a new `/audio` upload replaces them. New uploads are always encrypted while encryption is enabled.

Rerun `supabase_audio_storage.sql` so the private bucket permits `application/octet-stream` and has enough size allowance for authenticated-encryption overhead.
