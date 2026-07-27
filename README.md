# Ozo Donation API

FastAPI backend for supporter management, encrypted visit alerts, Supabase storage, and Telegram administration.

## Version 1.4.0 improvements

- Adds a Telegram supporter-management panel with inline buttons.
- Adds `➕ Add supporter` and `📋 Supporter list` buttons through `/manage`.
- Adds paginated supporter lists with visible/hidden status.
- Adds per-supporter `✏️ Update` and `🗑 Delete` buttons.
- Adds delete confirmation before removing a supporter.
- Adds guided add and update reply workflows with a 10-minute action timeout.
- Adds `/list`, `/supporters`, `/manage`, and `/cancel` commands.
- Supports partial updates such as `name=... | amount=... | payment=...`.
- Supports clearing optional fields with `message=none`, `avatar=none`, or `payment=none`.
- Keeps all management actions restricted to the configured chat and administrators.
- Configures Telegram to deliver both `message` and `callback_query` webhook updates.
- Keeps the existing secure and idempotent `/add` command.
- Includes all reliability, security, cache, retry, and Supabase fixes from version 1.3.2.

## Telegram supporter manager

Open the manager with:

```text
/manage
```

The bot displays these buttons:

- `➕ Add supporter`: starts a guided reply for supporter details.
- `📋 Supporter list`: shows supporters five at a time.
- `✏️ Update`: starts a guided partial-update reply for the selected supporter.
- `🗑 Delete`: opens a confirmation screen before deletion.
- `⬅️ Previous` and `Next ➡️`: navigate long supporter lists.
- `🔄 Refresh`: reloads the current page.

You can also open the list directly:

```text
/list
/supporters
```

To update a supporter after pressing `✏️ Update`, reply with only the fields that
should change:

```text
name=New Name | amount=20 | currency=USD | payment=ABA
```

Available update fields:

```text
name, amount, currency, message, avatar, payment, visible
```

Examples:

```text
message=Thank you for your support
avatar=https://example.com/avatar.jpg
payment=none
visible=false
```

Use `/cancel` to cancel an active add or update action. Pending actions expire
automatically after 10 minutes.

After upgrading, redeploy with `TELEGRAM_AUTO_CONFIGURE_WEBHOOK=true`, or run:

```bash
python scripts/configure_telegram.py
```

This is required once so Telegram starts sending `callback_query` updates for the
new buttons. No database migration is required when upgrading from version 1.3.2.

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

## Upgrade notes

Upgrading from version 1.3.2 to 1.4.0 does not require a database migration. Redeploy the backend so the webhook is reconfigured to accept button callback updates.

For versions older than 1.3.2, run `supabase_migration_v1_3_2.sql` once in the Supabase SQL Editor. It replaces the partial Telegram update index with a normal unique index so PostgREST can use:

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

The included regression suite covers application startup, request-body limits, proxy parsing, supporter validation, Telegram add/list/update/delete button workflows, callback authorization, webhook configuration, retries, supporter cache concurrency, visit delivery retries, and bounded token-bucket rate limiting. The current suite contains 43 tests.
