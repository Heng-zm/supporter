# Audio storage 503 fix — v2.1.3

This release fixes the generic response:

```json
{"detail":"The audio storage service is temporarily unavailable."}
```

## Fixed

- Accepts `SUPABASE_KEY` in addition to `SUPABASE_SECRET_KEY` and `SUPABASE_SERVICE_ROLE_KEY`.
- Accepts `SUPABASE_PROJECT_URL` as an alias for `SUPABASE_URL`.
- Handles Supabase Storage object-not-found responses that use HTTP 400 with a JSON `statusCode` of 404.
- Automatically creates the private `website-audio` bucket when the server key permits it.
- Returns `available: false` when `current.json` has not been created yet.
- Adds safe error codes to 503 responses and `/health` diagnostics.

## Render variables

Use the variables already used by the supporter backend:

```env
SUPABASE_URL=https://YOUR_PROJECT.supabase.co
SUPABASE_SERVICE_ROLE_KEY=YOUR_SERVER_KEY
```

This also works:

```env
SUPABASE_URL=https://YOUR_PROJECT.supabase.co
SUPABASE_KEY=YOUR_SERVER_KEY
```

Do not use a browser publishable/anonymous key for the private audio bucket.

## Expected response before the first Telegram upload

```json
{"ok":true,"available":false}
```

After deployment, send an audio file to the Telegram bot with `/audio`.
