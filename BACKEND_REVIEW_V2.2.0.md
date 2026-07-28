# Backend review — v2.2.0

This release changes only verified backend behavior. It preserves the public audio endpoints, Telegram `/audio` commands, source-controlled non-secret settings, private Supabase bucket, and solid-white React frontend contract.

## Correctness fixes

- Empty `current.json` results are cached for the configured metadata TTL.
- Listed Telegram administrators can use the bot in a private chat even when `TELEGRAM_CHAT_ID` points to a group.
- Concurrent processing of the same Telegram update is serialized and returns the same active version.
- Immutable version uploads no longer use upsert.
- If `current.json` fails after a version upload, the new orphan object is removed on a best-effort basis.
- Publishable and legacy `anon` Supabase keys are rejected before Storage requests.
- Manifest values are validated strictly before any object path is used.

## Performance and resilience

- Telegram file downloads and Supabase object reads are streamed in bounded chunks.
- `Content-Length` and actual streamed bytes are both enforced.
- Supabase outage failures use a short retry backoff instead of checking the bucket on every request.
- Local Storage readiness is cached rather than recreating the directory on every request.
- `/api/audio/metadata` supports `If-None-Match` and returns `304 Not Modified` when unchanged.

## Security and diagnostics

- Local object paths are resolved and checked to remain inside the configured storage directory.
- Production health output exposes safe error codes but not internal storage error messages or webhook URLs.
- Temporary 503 responses include `Retry-After: 5`.

## Verification

```bash
python -m compileall -q app scripts tests demo_app.py
pytest -q
python scripts/self_check.py
python scripts/check_audio_routes.py
```

Expected test result:

```text
29 passed
```
