from __future__ import annotations

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def _production_settings(**overrides) -> Settings:
    values = {
        "app_environment": "production",
        "debug": False,
        "backend_cors_origins_raw": "https://frontend.example.com",
        "allowed_hosts_raw": "api.example.com",
        "trust_proxy_headers": False,
        "visit_hash_salt": "visit-salt-that-is-long-and-unique-123456",
        "require_encrypted_visits": False,
        "visit_alert_enabled": False,
    }
    values.update(overrides)
    return Settings(**values)


def test_security_headers_and_request_id_are_added() -> None:
    app = create_app(Settings(require_encrypted_visits=False, trust_proxy_headers=False))
    with TestClient(app) as client:
        response = client.get("/health", headers={"X-Request-ID": "valid-request-id-123"})

    assert response.status_code == 200
    assert response.headers["x-request-id"] == "valid-request-id-123"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "default-src 'none'" in response.headers["content-security-policy"]


def test_invalid_request_id_is_replaced() -> None:
    app = create_app(Settings(require_encrypted_visits=False, trust_proxy_headers=False))
    with TestClient(app) as client:
        response = client.get("/health", headers={"X-Request-ID": "bad\nvalue"})

    assert response.status_code == 200
    assert response.headers["x-request-id"] != "bad\nvalue"
    assert len(response.headers["x-request-id"]) == 32


def test_production_disables_docs_and_hides_health_details() -> None:
    app = create_app(_production_settings())
    with TestClient(app, base_url="https://api.example.com") as client:
        docs = client.get("/docs")
        health = client.get("/health")
        root = client.get("/")

    assert docs.status_code == 404
    assert health.status_code == 200
    assert "environment" not in health.json()
    assert "supabaseConfigured" not in health.json()
    assert "docs" not in root.json()
    assert health.headers["strict-transport-security"] == "max-age=31536000"


def test_production_rejects_plain_http_api_requests() -> None:
    app = create_app(_production_settings())
    with TestClient(app, base_url="http://api.example.com") as client:
        response = client.get("/api/supporters")
        health = client.get("/health")

    assert response.status_code == 400
    assert response.json()["detail"] == "HTTPS is required."
    assert health.status_code == 200


def test_admin_api_is_hidden_when_disabled() -> None:
    app = create_app(Settings(require_encrypted_visits=False, trust_proxy_headers=False))
    with TestClient(app) as client:
        response = client.post(
            "/api/supporters",
            headers={"X-Admin-Key": "not-used", "Content-Type": "application/json"},
            json={"name": "Alice", "amount": 1, "currency": "USD"},
        )

    assert response.status_code == 404


def test_visit_endpoint_requires_json_content_type() -> None:
    app = create_app(Settings(require_encrypted_visits=False, trust_proxy_headers=False))
    with TestClient(app) as client:
        response = client.post(
            "/api/website/visit",
            content=b"{}",
            headers={"Content-Type": "text/plain"},
        )

    assert response.status_code == 415


def test_admin_api_rejects_invalid_key_before_database_call() -> None:
    settings = Settings(
        require_encrypted_visits=False,
        trust_proxy_headers=False,
        supabase_url="https://project.supabase.co",
        supabase_secret_key="server-secret",
        supporters_admin_api_enabled=True,
        supporters_admin_key="admin-key-that-is-long-and-unique-123456",
    )
    app = create_app(settings)
    with TestClient(app) as client:
        response = client.post(
            "/api/supporters",
            headers={"X-Admin-Key": "wrong", "Content-Type": "application/json"},
            json={"name": "Alice", "amount": 1, "currency": "USD"},
        )

    assert response.status_code == 401
    assert response.headers["cache-control"] == "no-store"


def test_admin_api_ip_allowlist_is_enforced() -> None:
    settings = Settings(
        require_encrypted_visits=False,
        trust_proxy_headers=False,
        supabase_url="https://project.supabase.co",
        supabase_secret_key="server-secret",
        supporters_admin_api_enabled=True,
        supporters_admin_key="admin-key-that-is-long-and-unique-123456",
        admin_allowed_networks_raw="203.0.113.0/24",
    )
    app = create_app(settings)
    with TestClient(app) as client:
        response = client.post(
            "/api/supporters",
            headers={
                "X-Admin-Key": "admin-key-that-is-long-and-unique-123456",
                "Content-Type": "application/json",
            },
            json={"name": "Alice", "amount": 1, "currency": "USD"},
        )

    assert response.status_code == 404
