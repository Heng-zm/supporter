from __future__ import annotations

import asyncio
import os
import sys
from typing import Any

import httpx

TRUE_VALUES = {"1", "true", "yes", "on"}
REQUIRED_UPDATES = {"message", "callback_query"}
REQUIRED_COMMANDS = {"add", "command", "manage"}


def _env_enabled(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in TRUE_VALUES


def _telegram_payload(
    response: httpx.Response,
    method: str,
) -> dict[str, Any] | None:
    try:
        payload = response.json()
    except ValueError:
        print(f"Telegram {method} returned invalid JSON.", file=sys.stderr)
        return None

    if (
        response.status_code >= 400
        or not isinstance(payload, dict)
        or payload.get("ok") is not True
    ):
        print(f"Telegram {method} failed.", file=sys.stderr)
        return None
    return payload


async def main() -> int:
    if not _env_enabled("TELEGRAM_COMMANDS_ENABLED"):
        print(
            "TELEGRAM_COMMANDS_ENABLED must be true for supporter commands.",
            file=sys.stderr,
        )
        return 2

    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    expected_url = os.getenv("TELEGRAM_WEBHOOK_URL", "").strip().rstrip("/")
    if not token:
        print("TELEGRAM_BOT_TOKEN is missing.", file=sys.stderr)
        return 2
    if not expected_url:
        print("TELEGRAM_WEBHOOK_URL is missing.", file=sys.stderr)
        return 2

    timeout = httpx.Timeout(20.0, connect=10.0)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            webhook_response, commands_response = await asyncio.gather(
                client.post(
                    f"https://api.telegram.org/bot{token}/getWebhookInfo",
                    json={},
                ),
                client.post(
                    f"https://api.telegram.org/bot{token}/getMyCommands",
                    json={},
                ),
            )
    except httpx.HTTPError:
        print("Telegram API could not be reached.", file=sys.stderr)
        return 1

    webhook_payload = _telegram_payload(webhook_response, "getWebhookInfo")
    commands_payload = _telegram_payload(commands_response, "getMyCommands")
    if webhook_payload is None or commands_payload is None:
        return 1

    webhook = webhook_payload.get("result")
    if not isinstance(webhook, dict):
        print("Telegram returned invalid webhook information.", file=sys.stderr)
        return 1

    actual_url = str(webhook.get("url") or "")
    pending = webhook.get("pending_update_count")
    error = webhook.get("last_error_message")
    allowed_updates = webhook.get("allowed_updates")

    if actual_url.rstrip("/") != expected_url:
        print("Webhook URL mismatch.", file=sys.stderr)
        print(f"Expected: {expected_url}", file=sys.stderr)
        print(f"Actual:   {actual_url or '(empty)'}", file=sys.stderr)
        return 1

    configured_updates: set[str] | None = None
    if isinstance(allowed_updates, list):
        configured_updates = {str(item) for item in allowed_updates}
        missing_updates = REQUIRED_UPDATES - configured_updates
        if missing_updates:
            print(
                "Webhook is missing required update types: "
                f"{', '.join(sorted(missing_updates))}",
                file=sys.stderr,
            )
            return 1

    command_rows = commands_payload.get("result")
    if not isinstance(command_rows, list):
        print("Telegram returned an invalid command list.", file=sys.stderr)
        return 1
    registered_commands = {
        str(row.get("command") or "")
        for row in command_rows
        if isinstance(row, dict)
    }
    missing_commands = REQUIRED_COMMANDS - registered_commands
    if missing_commands:
        print(
            "Telegram command menu is missing: "
            f"{', '.join(sorted(missing_commands))}",
            file=sys.stderr,
        )
        return 1

    print("Telegram supporter commands are configured correctly.")
    print(f"Webhook URL: {actual_url}")
    print(
        "Allowed updates: "
        + (
            ", ".join(sorted(configured_updates))
            if configured_updates is not None
            else "all (Telegram omitted the restriction)"
        )
    )
    print(f"Registered commands: {', '.join(sorted(registered_commands))}")
    print(f"Pending updates: {pending}")
    if error:
        print(f"Last Telegram delivery error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
