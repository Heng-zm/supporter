from __future__ import annotations

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import APP_VERSION, create_app
from app.problems import _validation_pointer
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
        assert "/api/v1/supporters" in paths
        assert "/api/website/visit" in paths
        assert "/api/v1/website/visit" in paths
        assert "/api/v1/audio/metadata" in paths
        assert "/api/telegram/webhook" in paths
        assert "/health/live" in paths
        assert "/health/ready" in paths


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
        assert response.headers["content-type"].startswith("application/problem+json")
        assert response.json()["errorCode"] == "payload_too_large"


def test_render_blueprint_uses_readiness_health_check() -> None:
    from pathlib import Path

    source = (Path(__file__).parents[1] / "render.yaml").read_text(encoding="utf-8")
    assert "runtime: docker" in source
    assert "plan: free" in source
    assert "healthCheckPath: /health/ready" in source


def test_openapi_documents_v1_and_problem_details_only() -> None:
    settings = Settings(
        require_encrypted_visits=False,
        trust_proxy_headers=False,
        enable_api_docs=True,
    )
    app = create_app(settings)
    with TestClient(app) as client:
        schema = client.get("/openapi.json").json()

    paths = schema["paths"]
    assert "/api/v1/supporters" in paths
    assert "/api/v1/audio/metadata" in paths
    assert "/api/supporters" not in paths
    assert "/api/audio/metadata" not in paths
    error_content = paths["/api/v1/supporters"]["get"]["responses"]["422"][
        "content"
    ]
    assert set(error_content) == {"application/problem+json"}


def test_unhandled_errors_are_safe_problem_details() -> None:
    settings = Settings(require_encrypted_visits=False, trust_proxy_headers=False)
    app = create_app(settings)

    @app.get("/test-unhandled", include_in_schema=False)
    async def test_unhandled() -> None:
        raise RuntimeError("sensitive internal detail")

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/test-unhandled")

    assert response.status_code == 500
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["errorCode"] == "internal_server_error"
    assert "sensitive internal detail" not in response.text


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


async def test_body_limit_rejects_malformed_content_length() -> None:
    from app.middleware.body_limit import RequestBodyLimitMiddleware

    async def receive() -> dict[str, object]:
        raise AssertionError("A rejected request body must not be read.")

    async def downstream(scope, receive, send) -> None:
        raise AssertionError("A malformed request must not reach the application.")

    sent: list[dict[str, object]] = []

    async def send(message: dict[str, object]) -> None:
        sent.append(message)

    for headers in (
        [(b"content-length", b"-1")],
        [(b"content-length", b"invalid")],
        [(b"content-length", b"1"), (b"content-length", b"2")],
    ):
        sent.clear()

        middleware = RequestBodyLimitMiddleware(downstream, max_bytes=4096)
        await middleware(
            {
                "type": "http",
                "method": "POST",
                "path": "/",
                "headers": headers,
                "query_string": b"",
                "server": ("test", 80),
                "client": ("127.0.0.1", 1234),
                "scheme": "http",
            },
            receive,
            send,
        )

        assert sent[0]["type"] == "http.response.start"
        assert sent[0]["status"] == 400
        assert b"application/problem+json" in dict(sent[0]["headers"])[b"content-type"]
        assert b"invalid_content_length" in sent[1]["body"]


async def test_body_limit_rejects_huge_numeric_content_length_without_parsing() -> None:
    from app.middleware.body_limit import RequestBodyLimitMiddleware

    sent: list[dict[str, object]] = []

    async def receive() -> dict[str, object]:
        raise AssertionError("A rejected request body must not be read.")

    async def send(message: dict[str, object]) -> None:
        sent.append(message)

    async def downstream(scope, receive, send) -> None:
        raise AssertionError("An oversized request must not reach the application.")

    middleware = RequestBodyLimitMiddleware(downstream, max_bytes=4096)
    await middleware(
        {
            "type": "http",
            "method": "POST",
            "path": "/",
            "headers": [(b"content-length", b"9" * 5000)],
            "query_string": b"",
            "server": ("test", 80),
            "client": ("127.0.0.1", 1234),
            "scheme": "http",
        },
        receive,
        send,
    )

    assert sent[0]["status"] == 413
    assert b"payload_too_large" in sent[1]["body"]


def test_validation_pointer_only_removes_the_location_prefix() -> None:
    assert _validation_pointer(("body", "query", "value")) == "/query/value"
    assert _validation_pointer(("query", "body")) == "/body"
