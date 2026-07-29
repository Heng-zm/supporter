from __future__ import annotations

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import APP_VERSION, create_app
from app.services.telegram_commands import TelegramCommandService


def test_health_and_routes() -> None:
    settings = Settings(
        require_encrypted_visits=False,
        trust_proxy_headers=False,
    )
    app = create_app(settings)
    with TestClient(app) as client:
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json()["ok"] is True
        paths = {route.path for route in app.routes}
        assert "/api/supporters" in paths
        assert "/api/website/visit" in paths
        assert "/api/telegram/webhook" in paths


def test_root_supports_browser_and_api_clients() -> None:
    settings = Settings(
        require_encrypted_visits=False,
        trust_proxy_headers=False,
    )
    app = create_app(settings)
    with TestClient(app) as client:
        browser_response = client.get("/", headers={"Accept": "text/html"})
        api_response = client.get("/", headers={"Accept": "application/json"})

    assert browser_response.status_code == 200
    assert browser_response.headers["content-type"].startswith("text/html")
    assert "Donation and audio, delivered reliably." in browser_response.text
    assert APP_VERSION in browser_response.text
    assert "style-src 'sha256-" in browser_response.headers["content-security-policy"]

    assert api_response.status_code == 200
    assert api_response.json()["status"] == "operational"
    assert api_response.json()["version"] == APP_VERSION


def test_body_limit_rejects_large_payload() -> None:
    settings = Settings(
        require_encrypted_visits=False,
        trust_proxy_headers=False,
        max_request_body_bytes=4096,
    )
    app = create_app(settings)
    with TestClient(app) as client:
        response = client.post(
            "/api/website/visit",
            content=b"x" * 5000,
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 413


def test_production_rejects_malformed_required_encryption_key() -> None:
    settings = Settings(
        app_environment="production",
        visit_hash_salt="a-secure-production-visit-salt-123",
        visit_private_key_b64="not-valid-base64",
        require_encrypted_visits=True,
        visit_alert_enabled=False,
        trust_proxy_headers=False,
        backend_cors_origins_raw="https://frontend.example.com",
        allowed_hosts_raw="api.example.com",
    )
    app = create_app(settings)
    try:
        with TestClient(app):
            pass
    except RuntimeError as exc:
        assert "Encrypted visits are required but unavailable" in str(exc)
    else:
        raise AssertionError("Production startup should reject an invalid encryption key.")


def test_packaged_webhook_dispatches_supporter_commands() -> None:
    settings = Settings(
        app_environment="test",
        require_encrypted_visits=False,
        trust_proxy_headers=False,
        supabase_url="https://test.supabase.co",
        supabase_secret_key="server-secret",
        telegram_bot_token="test-bot-token",
        telegram_chat_id="123",
        telegram_commands_enabled=True,
        telegram_webhook_secret="telegram-webhook-secret-123456",
    )
    app = create_app(settings)
    handled_updates: list[int] = []

    class RecordingCommands:
        async def handle(self, update) -> None:
            handled_updates.append(update.update_id)

    with TestClient(app) as client:
        assert isinstance(app.state.telegram_commands, TelegramCommandService)
        app.state.telegram_commands = RecordingCommands()
        response = client.post(
            "/api/telegram/webhook",
            headers={
                "Content-Type": "application/json",
                "X-Telegram-Bot-Api-Secret-Token": (
                    "telegram-webhook-secret-123456"
                ),
            },
            json={
                "update_id": 501,
                "message": {
                    "message_id": 10,
                    "from": {
                        "id": 123,
                        "is_bot": False,
                        "first_name": "Admin",
                    },
                    "chat": {"id": 123, "type": "private"},
                    "text": "/help",
                },
            },
        )
        duplicate = client.post(
            "/api/telegram/webhook",
            headers={
                "Content-Type": "application/json",
                "X-Telegram-Bot-Api-Secret-Token": (
                    "telegram-webhook-secret-123456"
                ),
            },
            json={
                "update_id": 501,
                "message": {
                    "message_id": 10,
                    "from": {
                        "id": 123,
                        "is_bot": False,
                        "first_name": "Admin",
                    },
                    "chat": {"id": 123, "type": "private"},
                    "text": "/help",
                },
            },
        )

    assert response.status_code == 200
    assert response.json() == {"ok": True, "handled": True}
    assert duplicate.status_code == 200
    assert duplicate.json()["duplicate"] is True
    assert handled_updates == [501]


def test_packaged_add_command_creates_supporter() -> None:
    settings = Settings(
        app_environment="test",
        require_encrypted_visits=False,
        trust_proxy_headers=False,
        supabase_url="https://test.supabase.co",
        supabase_secret_key="server-secret",
        telegram_bot_token="test-bot-token",
        telegram_chat_id="123",
        telegram_commands_enabled=True,
        telegram_webhook_secret="telegram-webhook-secret-123456",
    )
    app = create_app(settings)
    created_rows: list[tuple[dict[str, object], int]] = []
    replies: list[str] = []

    class RecordingSupabase:
        enabled = True

        async def create_supporter_from_telegram(
            self,
            row: dict[str, object],
            update_id: int,
        ) -> dict[str, object]:
            created_rows.append((row, update_id))
            return {
                "id": "supporter-telegram-1",
                **row,
            }

    class RecordingTelegram:
        async def send_message(
            self,
            _chat_id,
            text: str,
            **_kwargs,
        ) -> object:
            replies.append(text)
            return object()

    with TestClient(app) as client:
        app.state.telegram_commands = TelegramCommandService(
            settings,
            RecordingSupabase(),  # type: ignore[arg-type]
            RecordingTelegram(),  # type: ignore[arg-type]
        )
        response = client.post(
            "/api/telegram/webhook",
            headers={
                "Content-Type": "application/json",
                "X-Telegram-Bot-Api-Secret-Token": (
                    "telegram-webhook-secret-123456"
                ),
            },
            json={
                "update_id": 601,
                "message": {
                    "message_id": 20,
                    "from": {
                        "id": 123,
                        "is_bot": False,
                        "first_name": "Admin",
                    },
                    "chat": {"id": 123, "type": "private"},
                    "text": "/add@OzoDonationBot Alice | 12.50 | USD | Thanks",
                },
            },
        )

    assert response.status_code == 200
    assert response.json() == {"ok": True, "handled": True}
    assert created_rows == [
        (
            {
                "name": "Alice",
                "amount": 12.5,
                "currency": "USD",
                "message": "Thanks",
                "avatar_url": None,
                "payment_method": None,
            },
            601,
        )
    ]
    assert "Supporter added" in replies[0]


async def test_body_limit_rejects_chunked_payload_without_content_length() -> None:
    from app.middleware.body_limit import RequestBodyLimitMiddleware

    sent: list[dict[str, object]] = []
    incoming = iter(
        [
            {"type": "http.request", "body": b"123", "more_body": True},
            {"type": "http.request", "body": b"456", "more_body": False},
        ]
    )

    async def receive() -> dict[str, object]:
        return next(incoming)

    async def send(message: dict[str, object]) -> None:
        sent.append(message)

    async def downstream(scope, receive, send) -> None:
        while True:
            message = await receive()
            if not message.get("more_body", False):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    middleware = RequestBodyLimitMiddleware(downstream, max_bytes=5)
    await middleware(
        {
            "type": "http",
            "method": "POST",
            "path": "/",
            "headers": [],
            "query_string": b"",
            "server": ("test", 80),
            "client": ("127.0.0.1", 1234),
            "scheme": "http",
        },
        receive,
        send,
    )

    assert sent[0]["type"] == "http.response.start"
    assert sent[0]["status"] == 413
