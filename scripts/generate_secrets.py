from __future__ import annotations

import secrets

for name in (
    "VISIT_HASH_SALT",
    "TELEGRAM_WEBHOOK_SECRET",
    "SUPPORTERS_ADMIN_KEY",
):
    print(f"{name}={secrets.token_urlsafe(48)}")
