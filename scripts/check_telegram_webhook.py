from __future__ import annotations

import asyncio
import os
import sys

import httpx


async def main() -> int:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    expected_url = os.getenv("TELEGRAM_WEBHOOK_URL", "").strip().rstrip("/")
    if not token:
        print("TELEGRAM_BOT_TOKEN is missing.", file=sys.stderr)
        return 2
    if not expected_url:
        print("TELEGRAM_WEBHOOK_URL is missing.", file=sys.stderr)
        return 2

    timeout = httpx.Timeout(20.0, connect=10.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(
            f"https://api.telegram.org/bot{token}/getWebhookInfo",
            json={},
        )

    try:
        payload = response.json()
    except ValueError:
        print("Telegram returned invalid JSON.", file=sys.stderr)
        return 1

    result = payload.get("result") if isinstance(payload, dict) else None
    actual_url = str(result.get("url") or "") if isinstance(result, dict) else ""
    pending = result.get("pending_update_count") if isinstance(result, dict) else None
    error = result.get("last_error_message") if isinstance(result, dict) else None

    if response.status_code >= 400 or payload.get("ok") is not True:
        print("Telegram getWebhookInfo failed.", file=sys.stderr)
        return 1
    if actual_url.rstrip("/") != expected_url:
        print("Webhook URL mismatch.", file=sys.stderr)
        print(f"Expected: {expected_url}", file=sys.stderr)
        print(f"Actual:   {actual_url or '(empty)'}", file=sys.stderr)
        return 1

    print("Telegram webhook is configured correctly.")
    print(f"URL: {actual_url}")
    print(f"Pending updates: {pending}")
    if error:
        print(f"Last Telegram delivery error: {error}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
