from __future__ import annotations

import base64
import json

import pytest
from pydantic import ValidationError

from app.config import Settings


def _jwt(payload: dict[str, object]) -> str:
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"header.{encoded}.signature"


def _production_values() -> dict[str, object]:
    return {
        "app_environment": "production",
        "backend_cors_origins_raw": "https://frontend.example.com",
        "allowed_hosts_raw": "api.example.com",
        "trust_proxy_headers": False,
        "visit_hash_salt": "visit-salt-that-is-long-and-unique-123456",
        "require_encrypted_visits": False,
        "visit_alert_enabled": False,
    }


def test_production_rejects_wildcard_host() -> None:
    values = _production_values()
    values["allowed_hosts_raw"] = "*"
    with pytest.raises(ValidationError, match="ALLOWED_HOSTS"):
        Settings(**values)


def test_production_rejects_insecure_cors_origin() -> None:
    values = _production_values()
    values["backend_cors_origins_raw"] = "http://frontend.example.com"
    with pytest.raises(ValidationError, match="HTTPS"):
        Settings(**values)


def test_production_rejects_public_supabase_jwt() -> None:
    values = _production_values()
    values.update(
        {
            "supabase_url": "https://project.supabase.co",
            "supabase_secret_key": _jwt({"role": "anon"}),
        }
    )
    with pytest.raises(ValidationError, match="service-role"):
        Settings(**values)


def test_production_admin_api_requires_ip_allowlist() -> None:
    values = _production_values()
    values.update(
        {
            "supabase_url": "https://project.supabase.co",
            "supabase_secret_key": "sb_secret_server_key",
            "supporters_admin_api_enabled": True,
            "supporters_admin_key": "admin-key-that-is-long-and-unique-123456",
        }
    )
    with pytest.raises(ValidationError, match="ADMIN_ALLOWED_NETWORKS"):
        Settings(**values)


def test_production_rejects_reused_secrets() -> None:
    values = _production_values()
    repeated = "same-secret-that-is-long-enough-123456789"
    values.update(
        {
            "visit_hash_salt": repeated,
            "supporters_admin_key": repeated,
        }
    )
    with pytest.raises(ValidationError, match="different secrets"):
        Settings(**values)
