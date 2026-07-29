# Ozo Donation API v2.4.2-audio — high-security server

FastAPI backend for the public supporter list, encrypted visit notifications,
and the Telegram step-by-step supporter manager.

## Security upgrade summary

- Production rejects wildcard hosts, insecure CORS origins, weak secrets, and reused secrets.
- HTTPS is enforced for API traffic behind explicitly trusted proxies.
- HSTS, CSP, clickjacking, MIME-sniffing, referrer, permissions, and robot-blocking headers are added.
- Swagger, ReDoc, and OpenAPI are disabled in production unless explicitly enabled.
- Every response receives a validated `X-Request-ID`; logs exclude query strings, bodies, and credentials.
- REST supporter administration is disabled by default.
- When REST administration is enabled, it requires an API key, IP allowlist, and rate limiting.
- Telegram webhooks require JSON, the secret header, optional source-IP filtering, and feature-level rate limiting.
- One replay-protected webhook safely dispatches both audio management and supporter commands.
- Telegram webhook auto-configuration is opt-in and includes callback queries when commands are enabled.
- Telegram network errors no longer risk writing the bot token into logs.
- Production Supabase errors no longer log provider details that may contain submitted data.
- Public avatar URLs must use HTTPS and cannot point to localhost or private literal IP addresses.
- Visit URLs and referrers drop query strings and fragments by default.
- Client-controlled visit IDs and timestamps are replaced with server-generated values.
- Uvicorn runs without proxy trust, access logs, or a server banner and uses bounded concurrency and request recycling.
- Docker runs as a non-root user and validates installed Python dependencies during image creation.

Version 2.4.0 adds structured website-visit analytics. Run
`supabase/supabase_migration_v2_4_0_visit_analytics.sql` in the Supabase SQL
Editor before deploying this release.

Version 2.4.2 restores the `/command` and `/commands` supporter-manager aliases,
improves Telegram replay/action cache performance, and adds deployment checks
for the webhook update types and registered bot commands.

## Deploy safely

Copy `.env.example` into Render environment variables and replace every placeholder.
The most important values are:

```env
APP_ENVIRONMENT=production
BACKEND_CORS_ORIGINS=https://your-frontend.example.com
ALLOWED_HOSTS=your-backend.example.com
ENFORCE_HTTPS=true
ENABLE_API_DOCS=false

SUPABASE_URL=https://YOUR_PROJECT.supabase.co
SUPABASE_SECRET_KEY=YOUR_SERVICE_ROLE_OR_SERVER_SECRET

TELEGRAM_COMMANDS_ENABLED=true
TELEGRAM_BOT_TOKEN=YOUR_BOT_TOKEN
TELEGRAM_CHAT_ID=YOUR_NUMERIC_CHAT_ID
TELEGRAM_ADMIN_USER_IDS=YOUR_NUMERIC_TELEGRAM_USER_ID
TELEGRAM_WEBHOOK_SECRET=YOUR_UNIQUE_SECRET
TELEGRAM_WEBHOOK_URL=https://your-backend.example.com/api/telegram/webhook
TELEGRAM_AUTO_CONFIGURE_WEBHOOK=true

VISIT_HASH_SALT=ANOTHER_UNIQUE_SECRET
VISIT_PRIVATE_KEY_B64=YOUR_BASE64_RSA_PRIVATE_KEY
REQUIRE_ENCRYPTED_VISITS=true
```

Generate distinct secrets and the RSA key:

```bash
python scripts/generate_secrets.py
python scripts/generate_visit_key.py
```

Validate the final environment before deployment:

```bash
python scripts/security_check.py
```

## Important behavior changes

### REST admin API

It is now hidden by default:

```env
SUPPORTERS_ADMIN_API_ENABLED=false
```

The Telegram `/manage` buttons, guided `/add`, update, list, and delete flows
continue to work. To enable REST administration, configure all of these:

```env
SUPPORTERS_ADMIN_API_ENABLED=true
SUPPORTERS_ADMIN_KEY=A_DISTINCT_RANDOM_SECRET_AT_LEAST_32_CHARACTERS
ADMIN_ALLOWED_NETWORKS=YOUR_PUBLIC_ADMIN_IP/32
ADMIN_CORS_ENABLED=false
```

Keep `ADMIN_CORS_ENABLED=false` unless a trusted browser application truly
needs direct admin API access.

### Avatar URLs

Avatar URLs must start with `https://`. Localhost and private literal IP
addresses are rejected.

### Visit privacy

The default strips sensitive query parameters and fragments from page URLs and
referrers before storage or Telegram delivery:

```env
VISIT_STORE_URL_QUERY=false
VISIT_DETAILED_ANALYTICS_ENABLED=true
```

Detailed analytics accept bounded campaign attribution, navigation timing,
network quality, device capability, and first-party session context. Supplied
session IDs are salted and hashed before storage or Telegram delivery. Only
allowlisted `utm_source`, `utm_medium`, `utm_campaign`, `utm_id`, `utm_term`,
and `utm_content` query values are retained; all other query parameters remain
excluded when `VISIT_STORE_URL_QUERY=false`.

The encrypted visit payload may include:

```json
{
  "connection": {
    "type": "wifi",
    "effectiveType": "4g",
    "downlinkMbps": 25.5,
    "rttMs": 42,
    "saveData": false
  },
  "navigation": {
    "type": "navigate",
    "durationMs": 900,
    "domContentLoadedMs": 480,
    "loadTimeMs": 760,
    "transferSizeBytes": 120000
  },
  "capabilities": {
    "memoryGb": 8,
    "logicalProcessors": 8,
    "maxTouchPoints": 5,
    "colorDepth": 24,
    "cookiesEnabled": true,
    "doNotTrack": false
  },
  "session": {
    "id": "first-party-session-id",
    "pageViews": 4,
    "returningVisitor": true
  }
}
```

The website must add these fields to the existing plaintext visit payload
before encryption. A browser collector can use the following values after any
analytics consent required by your jurisdiction:

```js
const navigation = performance.getEntriesByType("navigation")[0];
const network = navigator.connection;
const existingSession = localStorage.getItem("visit_session_id");
const sessionId = existingSession || crypto.randomUUID();
const pageViews = Number(localStorage.getItem("visit_page_views") || "0") + 1;

localStorage.setItem("visit_session_id", sessionId);
localStorage.setItem("visit_page_views", String(pageViews));

const detailedVisitData = {
  connection: {
    online: navigator.onLine,
    type: network?.type || "Unknown",
    effectiveType: network?.effectiveType || "Unknown",
    downlinkMbps: network?.downlink ?? null,
    rttMs: network?.rtt ?? null,
    saveData: network?.saveData || false
  },
  navigation: navigation ? {
    type: navigation.type,
    redirectCount: navigation.redirectCount,
    durationMs: navigation.duration,
    domContentLoadedMs: navigation.domContentLoadedEventEnd,
    loadTimeMs: navigation.loadEventEnd,
    transferSizeBytes: navigation.transferSize
  } : {},
  capabilities: {
    memoryGb: navigator.deviceMemory ?? null,
    logicalProcessors: navigator.hardwareConcurrency ?? null,
    maxTouchPoints: navigator.maxTouchPoints || 0,
    colorDepth: screen.colorDepth,
    cookiesEnabled: navigator.cookieEnabled,
    doNotTrack: navigator.doNotTrack === "1"
  },
  session: {
    id: sessionId,
    pageViews,
    returningVisitor: Boolean(existingSession)
  }
};
```

### API documentation

Production documentation is disabled:

```env
ENABLE_API_DOCS=false
```

Temporarily enable it only on a protected non-production deployment.

## Telegram webhook

The webhook is configured with only `message` and `callback_query` updates,
a secret token, and a lower connection limit. Telegram management commands:

```text
/manage
/command
/commands
/list
/add
/cancel
/help
```

Both `/command` and `/commands` open the supporter manager. Commands only run
when `TELEGRAM_COMMANDS_ENABLED=true`, the message chat matches
`TELEGRAM_CHAT_ID`, and the sender is listed in `TELEGRAM_ADMIN_USER_IDS`.
Existing Supabase deployments must also run
`supabase/supabase_migration_v1_3_2.sql` so repeated Telegram deliveries can be
handled idempotently.

Do not add a Telegram source-IP allowlist unless you maintain accurate network
ranges. The secret header and rate limiter remain active when that setting is empty.

## Local development

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
pytest -q
python -m app.run
```

Production start command:

```bash
python -m app.run
```

## Verification

```bash
python -m compileall -q app tests
pytest -q
python scripts/security_check.py
python scripts/check_telegram_webhook.py
```

Application-level rate limiting is process-local. Keep `WEB_CONCURRENCY=1` on a
small Render service, and also enable Render/Cloudflare rate limits or a WAF for
stronger distributed denial-of-service protection.
