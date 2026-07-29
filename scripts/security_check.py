from __future__ import annotations

import sys
from pathlib import Path

# Allow direct execution from the project root without installing the app package.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pydantic import ValidationError  # noqa: E402

from app.config import Settings  # noqa: E402


def main() -> None:
    try:
        settings = Settings()
    except ValidationError as exc:
        print("Security configuration: FAILED")
        for error in exc.errors():
            print(f"- {error.get('msg', 'Invalid setting')}")
        raise SystemExit(1) from exc

    print("Security configuration: OK")
    print(f"- environment: {settings.app_environment}")
    print(f"- HTTPS enforcement: {settings.enforce_https}")
    print(f"- API docs enabled: {settings.enable_api_docs}")
    print(f"- explicit allowed hosts: {settings.allowed_hosts != ['*']}")
    print(f"- supporter REST admin API: {settings.supporters_admin_api_enabled}")
    print(f"- Supabase configured: {settings.supabase_enabled}")
    print(f"- Telegram bot configured: {settings.telegram_bot_enabled}")
    print(f"- Telegram commands enabled: {settings.telegram_commands_enabled}")
    print(
        "- Telegram command configuration complete: "
        f"{settings.telegram_commands_configured}"
    )
    print(
        f"- Telegram webhook URL configured: {bool(settings.telegram_webhook_url)}"
    )
    print(
        "- Telegram webhook auto-configuration: "
        f"{settings.telegram_auto_configure_webhook}"
    )
    print(f"- Telegram administrators: {len(settings.telegram_admin_user_ids)}")
    print(f"- encrypted visits required: {settings.require_encrypted_visits}")
    print(f"- visit URL query storage: {settings.visit_store_url_query}")


if __name__ == "__main__":
    main()
