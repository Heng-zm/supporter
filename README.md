# Ozo Donation API

FastAPI backend for supporter management, encrypted visit alerts, Supabase storage, and Telegram administration.

## Version 1.5.0 improvements

- Replaces the normal add form with a one-question-at-a-time Telegram wizard.
- Replaces the normal update form with the same guided reply workflow.
- Adds bilingual English/Khmer prompts for the main data-entry questions.
- Adds one-tap buttons for USD, KHR, ABA, ACLEDA, Cash, visible, and hidden.
- Adds `Skip`, `Keep current`, `Clear`, `Back`, and `Cancel` controls.
- Shows a complete confirmation screen before creating or updating a supporter.
- Revalidates the complete supporter record immediately before saving.
- Keeps the compact `/add Name | Amount | ...` and `name=value | ...` formats for experienced administrators.
- Keeps list pagination, update buttons, delete confirmation, authorization, retry handling, and database idempotency from version 1.4.0.

## Telegram supporter manager

Open the manager with:

```text
/manage
```

The bot displays:

- `➕ Add supporter`: starts the guided add wizard.
- `📋 Supporter list`: shows supporters five at a time.
- `✏️ Update`: starts the guided update wizard for a selected supporter.
- `🗑 Delete`: asks for confirmation before deletion.
- `⬅️ Previous`, `Next ➡️`, and `🔄 Refresh`: navigate the list.

You can also open the list directly:

```text
/list
/supporters
```

### Guided add workflow

Press `➕ Add supporter` or send `/add`. The bot asks for one value at a time:

1. Name
2. Amount
3. Currency
4. Message
5. Avatar URL
6. Payment method
7. Public visibility
8. Final confirmation

Reply directly to each question. Buttons are provided for common choices such as
`USD`, `KHR`, `ABA`, `ACLEDA`, `Cash`, visible, and hidden.

Optional fields can be skipped. Before saving, the bot shows the complete record
and waits for `✅ Save`.

### Guided update workflow

Open the supporter list and press `✏️ Update`. The bot shows the current value at
each step. Use:

- `➡️ Keep current` to leave a value unchanged.
- `🧹 Clear` to remove an optional message, avatar, or payment method.
- `⬅️ Back` to return to the previous field.
- `❌ Cancel` to stop without saving.

The bot saves only fields that actually changed.

### Reply commands

These commands work while a guided form is active:

```text
/back
/skip
/cancel
```

Pending forms expire automatically after 10 minutes.

The older compact update format remains available after pressing `✏️ Update`:

```text
name=New Name | amount=20 | currency=USD | payment=ABA
```

After upgrading, redeploy with `TELEGRAM_AUTO_CONFIGURE_WEBHOOK=true`, or run:

```bash
python scripts/configure_telegram.py
```

No database migration is required when upgrading from version 1.3.2 or newer.

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

Upgrading from version 1.3.2 through 1.4.0 to 1.5.0 does not require a database migration. Redeploy the backend so the webhook is reconfigured to accept button callback updates.

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
