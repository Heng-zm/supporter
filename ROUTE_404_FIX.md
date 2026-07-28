# Fix for `GET /api/audio/metadata` returning 404

A 404 means FastAPI has no matching route. It is different from:

- `503`: the route exists but audio storage is not configured or initialized.
- `502`: the route exists but Supabase Storage is temporarily unavailable.
- `200` with `available: false`: the route works but no audio has been uploaded yet.

## Files to copy

Copy these into the existing supporter backend repository:

```text
app/audio_extension/
app/main.py
scripts/check_audio_routes.py
```

The included `app/main.py` is based on the current supporter backend structure and adds three required operations:

1. `include_audio_router(app, api_prefix=runtime_settings.api_prefix)` during app creation.
2. `await start_audio_extension(app)` during lifespan startup.
3. `await close_audio_extension(app)` during lifespan shutdown.

It also merges the two source-controlled Vercel CORS origins with any existing backend origins.

## Verify before deployment

From the backend directory:

```bash
python scripts/check_audio_routes.py
```

Expected output:

```text
Audio route check passed.
Registered: /api/audio/file
Registered: /api/audio/metadata
```

Run the existing tests, then deploy the same Git commit that contains these files.

## Render checks

Render must start the module that was changed:

```bash
uvicorn app.main:app --host 0.0.0.0 --port $PORT
```

After deployment, check:

```text
https://supporter-ipio.onrender.com/health
https://supporter-ipio.onrender.com/api/audio/metadata
```

Expected metadata responses after the route is mounted:

Before an upload:

```json
{"ok": true, "available": false}
```

When persistent Supabase storage is required but unavailable:

```json
{"detail": "Persistent audio storage is required, but Supabase Storage is not configured."}
```

That second response is HTTP 503, which confirms the route exists and the remaining problem is configuration rather than routing.
