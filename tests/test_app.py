from __future__ import annotations

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


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
        trust_proxy_headers=False,
    )
    app = create_app(settings)
    try:
        with TestClient(app):
            pass
    except RuntimeError as exc:
        assert "Encrypted visits are required but unavailable" in str(exc)
    else:
        raise AssertionError("Production startup should reject an invalid encryption key.")


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
