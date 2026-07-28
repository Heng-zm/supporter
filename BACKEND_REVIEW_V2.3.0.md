# Backend encryption review — v2.3.0

## Implemented requirements

- AES-256-GCM encryption before Supabase upload.
- Authenticated chunk streaming from `/api/audio/file`.
- New random nonce seed for every uploaded file.
- Environment-backed key versioning and rotation.
- Plaintext and ciphertext SHA-256 and byte-length validation.
- No plaintext temporary files.
- Best-effort orphan cleanup after failed upload or manifest publication.
- Encrypted history with Telegram history and rollback commands.
- Safe single byte-range handling with `If-Range` semantics.
- Maximum decrypted-output enforcement.

## Container security

The custom container uses independent AES-GCM authentication per chunk. A 96-bit nonce is formed from a random 128-bit file seed and a SHA-256-derived 96-bit chunk nonce. The authenticated data binds the canonical header, chunk index, and plaintext chunk length. A chunk is not yielded until its GCM tag verifies.

The complete container and complete plaintext are also checked against their manifest SHA-256 values and lengths. This provides corruption detection in addition to per-chunk authentication.

## Publication safety

The backend publishes in this order:

1. Validate and hash plaintext in memory.
2. Encrypt to an in-memory authenticated container.
3. Upload the immutable encrypted object.
4. Write bounded `history.json`.
5. Write `current.json` last.

A failure after upload triggers deletion of the randomized object. A failure while publishing `current.json` also attempts to restore the previous history manifest. Versions pruned beyond the configured history limit are deleted after successful publication.

## Compatibility

Legacy unencrypted manifests remain readable. Every new upload is encrypted when `AUDIO_ENCRYPTION_ENABLED` is true. Old key versions must remain in the backend environment while retained history objects still use them.

## Verified regression coverage

The 40-test backend suite includes local and mocked Supabase encrypted round trips, ciphertext tamper rejection, nonce uniqueness, key rotation, missing-key failure, Telegram history and rollback, byte ranges, `If-Range`, decrypted size limits, failed-upload cleanup, history retention cleanup, and all prior webhook/storage/CORS behavior.
