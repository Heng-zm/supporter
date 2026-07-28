# Telegram bot `/audio` webhook fix — v2.1.4

The earlier package mounted the public audio API but did not mount a Telegram
webhook endpoint in the deployed FastAPI application. As a result, Telegram had
nowhere to deliver `/audio` updates.

This version adds:

- `POST /api/telegram/webhook`.
- Exact validation of `X-Telegram-Bot-Api-Secret-Token`.
- Bounded replay protection using Telegram `update_id`.
- Automatic `setWebhook` registration during FastAPI startup.
- `getWebhookInfo` verification after registration.
- Health fields showing whether Telegram confirmed the webhook.

Required Render variables:

```env
TELEGRAM_BOT_TOKEN=YOUR_BOT_TOKEN
TELEGRAM_CHAT_ID=YOUR_CHAT_ID
TELEGRAM_ADMIN_USER_IDS=YOUR_NUMERIC_USER_ID
TELEGRAM_WEBHOOK_SECRET=GENERATE_A_RANDOM_URL_SAFE_SECRET
TELEGRAM_WEBHOOK_URL=https://supporter-ipio.onrender.com/api/telegram/webhook
TELEGRAM_AUTO_CONFIGURE_WEBHOOK=true
```

Use a webhook secret containing only letters, numbers, `_`, and `-`.
Do not paste the bot token or server secrets into chat or frontend code.

After deployment, inspect `/health` and confirm:

```json
{
  "audioTelegramWebhookRouteConfigured": true,
  "audioTelegramWebhookConfigured": true,
  "audioTelegramWebhookError": null
}
```

Then send `/audio status` to the configured bot.
