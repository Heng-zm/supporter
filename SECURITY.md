# Security policy

## Secrets

Never commit `.env`, Telegram bot tokens, Supabase server/service-role keys,
RSA private keys, webhook secrets, admin keys, or visit hashing salts.
Generate distinct values with:

```bash
python scripts/generate_secrets.py
python scripts/generate_visit_key.py
```

Rotate a secret immediately if it appears in source control, a browser bundle,
a screenshot, a public log, or a chat sent to an untrusted person.

## Production controls

The server rejects unsafe production configuration, including wildcard hosts,
HTTP CORS origins, weak/reused secrets, missing Telegram administrator IDs,
and an enabled REST admin API without an IP allowlist.

The supporter REST admin API is disabled by default. Prefer the authorized
Telegram manager. If REST administration is required, use a server-to-server
client, a 32+ character random key, and `ADMIN_ALLOWED_NETWORKS`.

## Reporting

Do not open a public issue containing credentials or exploitable details.
Privately send the affected version, reproduction steps, expected behavior,
and impact to the project owner. Remove all real secrets and personal data.
