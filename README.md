# Ozo Donation API

FastAPI backend for supporter management, encrypted visit alerts, Supabase storage, and Telegram administration.

## Version 1.3.2 improvements

- Fixes Supabase/PostgREST error `42P10` when Telegram `/add` uses `on_conflict=telegram_update_id`.
- Replaces the partial Telegram update index with a normal unique index that still permits multiple `NULL` values.
- Adds actionable Telegram replies for outdated schema, missing tables/columns, and denied Supabase credentials.
- Preserves Supabase HTTP status and PostgREST error codes in server logs without exposing secrets.
- Fixes the protected HTTP supporter-create path passing an invalid duplicate argument.
- Fixes Telegram `/add` parsing when the optional message is omitted before an avatar URL.
- Supports multiline `/add` commands and preserves explicit empty optional fields.
- Adds a secure Telegram `/add` command through a webhook.
- Prevents unexpected command failures from suppressing Telegram webhook retries.
- Keeps Telegram supporter creation idempotent across retries and restarts.
- Corrects the PostgreSQL unique index used by PostgREST `on_conflict` handling.
- Retries safe Telegram webhook configuration calls after temporary `429` or `5xx` responses.
- Rejects malformed Telegram success responses instead of treating them as successful.
- Replaces the fixed-window visit limiter with a bounded token bucket.
- Prevents the rate-limit cache from growing beyond its configured maximum.
- Allows supporter writes to continue during slow supporter-list reads.
- Prevents an older list response from overwriting cache data after a mutation.
- Adds a matching Supabase index for rolling visit-cooldown lookups.
- Retries idempotent visit-delivery status updates after transient Supabase failures.
- Handles oversized streamed request bodies that omit `Content-Length`.
- Configures Telegram webhook and command metadata concurrently during startup.
- Reduces Docker build context and avoids generating Python bytecode in the image.
- Removes generated caches and bytecode from the downloadable archive.

## Telegram `/add` command

Minimal form:

```text
/add John Doe | 25.00
```

Full form:

```text
/add Name | Amount | Currency | Message | Avatar URL | Payment method
```

Examples:

```text
/add Jane Doe | 1,250.50 | USD | Thank you! | https://example.com/avatar.jpg | ABA
/add Chuo Kimheng | 1.00 | USD | https://pay-coffee-topaz.vercel.app/favicon.ico | ABA
/add Chuo Kimheng | 1.00 | USD | | https://pay-coffee-topaz.vercel.app/favicon.ico | ABA
```

Only `Name` and `Amount` are required. Currency defaults to `USD`. When the fourth field starts with `http://` or `https://`, it is automatically treated as the avatar URL and the message is considered omitted. You can also leave optional fields empty explicitly between two separators (`| |`).

The command is accepted only when:

1. `TELEGRAM_COMMANDS_ENABLED=true`.
2. Telegram sends the configured webhook secret header.
3. The message comes from `TELEGRAM_CHAT_ID`.
4. The sender is in `TELEGRAM_ADMIN_USER_IDS`, or the command is used in the owner's private chat.

For group chats, `TELEGRAM_ADMIN_USER_IDS` is mandatory.

## Upgrade from the previous version

For the fastest fix, run `supabase_migration_v1_3_2.sql` once in the Supabase SQL Editor. It replaces the partial Telegram update index with a normal unique index so PostgREST can use:

```text
on_conflict=telegram_update_id
```

PostgreSQL normal unique indexes permit multiple `NULL` values, so supporters created outside Telegram remain valid. The migration is idempotent and refreshes PostgREST's schema cache. A fresh installation can run the complete `supabase_schema.sql` instead.

## Setup

1. Run `supabase_schema.sql` in the Supabase SQL editor.
2. Copy `.env.example` to `.env` and replace every placeholder. The template is intentionally minimal; cache, timeout, rate-limit, API-prefix, proxy-network, and other tuning values use defaults from `app/config.py`.
3. Generate the visit encryption key:

```bash
python scripts/generate_visit_key.py
```

4. Install and test:

```bash
python -m pip install -r requirements-dev.txt
pytest
ruff check .
```

5. Configure the Telegram webhook automatically:

```env
TELEGRAM_AUTO_CONFIGURE_WEBHOOK=true
TELEGRAM_WEBHOOK_URL=https://your-backend.example.com/api/telegram/webhook
```

Or configure it manually after deployment:

```bash
python scripts/configure_telegram.py
```

6. Start locally:

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000 --no-proxy-headers
```

## Render deployment

Use the included Dockerfile. Do not add `--forwarded-allow-ips='*'`. The application parses `X-Forwarded-For` only when the direct peer belongs to `TRUSTED_PROXY_NETWORKS`.

Keep `WEB_CONCURRENCY=1` on a small/free service unless multiple workers are deliberately required. Supabase provides the cross-restart visit check, while the in-memory cache provides the fastest rolling cooldown within each worker.

### Optional environment variables

Add these only to override built-in defaults:

- `SUPPORTERS_ADMIN_KEY`: enables protected HTTP create, update, and delete endpoints.
- `MAX_SUPPORTERS`, `SUPPORTERS_CACHE_TTL_SECONDS`, `SUPPORTERS_STALE_CACHE_SECONDS`: supporter-list tuning.
- `REQUEST_TIMEOUT_SECONDS`, `MAX_REQUEST_BODY_BYTES`, `VISIT_RATE_LIMIT_PER_MINUTE`, `VISIT_ALERT_COOLDOWN_MINUTES`: request and visit tuning.
- `TRUST_PROXY_HEADERS`, `TRUSTED_PROXY_NETWORKS`: proxy handling overrides.
- `REQUIRE_VISIT_STORAGE`, `REQUIRE_ENCRYPTED_VISITS`, `VISIT_ALERT_ENABLED`: feature-policy overrides.

Do not add an optional variable unless its default behavior needs to change.

## Security notes

- Never expose `SUPABASE_SECRET_KEY`, `SUPPORTERS_ADMIN_KEY`, the Telegram bot token, webhook secret, visit salt, or RSA private key to the frontend.
- Use a different random value for every secret.
- Restrict `BACKEND_CORS_ORIGINS` and `ALLOWED_HOSTS` in production.
- Do not put `*` in `TRUSTED_PROXY_NETWORKS`.
- Keep Telegram commands restricted to one configured chat and explicit administrator IDs for group chats.

## Validation

The included regression suite covers application startup, request-body limits, proxy parsing, supporter validation, Telegram command parsing and retries, supporter cache concurrency, visit delivery retries, and bounded token-bucket rate limiting.
