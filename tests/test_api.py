from __future__ import annotations

import base64
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import APP_VERSION, create_app
from app.services.rate_limit import TokenBucketRateLimiter
from app.services.telegram import TelegramResult
from app.services.visit_crypto import AAD, ENCRYPTION_NAME, VisitCryptoService


class FakeSupabase:
    enabled = True

    def __init__(self) -> None:
        self.supporters = [
            {
                "id": str(uuid4()),
                "name": "Sokha",
                "amount": 25,
                "currency": "USD",
                "created_at": "2026-07-25T00:00:00Z",
            }
        ]
        self.visit_ids: set[str] = set()

    async def list_supporters(self, limit: int):
        return self.supporters[:limit]

    async def list_supporters_resilient(self, limit: int):
        return self.supporters[:limit], "memory-cache", False

    async def create_supporter(self, row):
        created = {"id": str(uuid4()), "created_at": "2026-07-25T00:00:00Z", **row}
        self.supporters.append(created)
        return created

    async def update_supporter(self, supporter_id: str, patch):
        for row in self.supporters:
            if row["id"] == supporter_id:
                row.update(patch)
                return row
        return None

    async def delete_supporter(self, supporter_id: str):
        self.supporters = [row for row in self.supporters if row["id"] != supporter_id]

    async def insert_visit_once(self, row):
        if row["dedupe_key"] in self.visit_ids:
            return None
        self.visit_ids.add(row["dedupe_key"])
        return {"id": str(uuid4())}

    async def find_recent_visit(self, **kwargs):
        return None

    async def update_visit_delivery(self, *args, **kwargs):
        return None


class FakeTelegram:
    async def send_visit(self, visit):
        return TelegramResult(ok=True, message_id="123")


@asynccontextmanager
async def noop_lifespan(app):
    yield


def make_private_key_b64() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return base64.b64encode(pem).decode("ascii")


def build_client(supabase=None) -> TestClient:
    admin_key = "test-admin-key-that-is-long-enough-123456"
    settings = Settings(
        app_environment="test",
        supabase_url="https://test.supabase.co",
        supabase_secret_key="test-server-secret",
        supporters_admin_api_enabled=True,
        supporters_admin_key=admin_key,
        visit_hash_salt="test-hash-salt",
        visit_private_key_b64=make_private_key_b64(),
        require_encrypted_visits=True,
        backend_cors_origins_raw="http://localhost:3000",
    )
    app = create_app(settings)
    app.router.lifespan_context = noop_lifespan
    app.state.settings = settings
    app.state.supabase = supabase or FakeSupabase()
    app.state.telegram = FakeTelegram()
    app.state.visit_crypto = VisitCryptoService(settings)
    app.state.admin_rate_limiter = TokenBucketRateLimiter()
    from app.services.visits import VisitService

    app.state.visits = VisitService(settings, app.state.supabase, app.state.telegram)
    return TestClient(app)


def encrypt_for_backend(public_key_b64: str, payload: dict) -> dict[str, str]:
    public_key = serialization.load_der_public_key(base64.b64decode(public_key_b64))
    aes_key = AESGCM.generate_key(bit_length=256)
    iv = b"123456789012"
    ciphertext = AESGCM(aes_key).encrypt(iv, json.dumps(payload).encode("utf-8"), AAD)
    encrypted_key = public_key.encrypt(
        aes_key,
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        ),
    )
    return {
        "encryption": ENCRYPTION_NAME,
        "encryptedKey": base64.b64encode(encrypted_key).decode("ascii"),
        "iv": base64.b64encode(iv).decode("ascii"),
        "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
    }


def test_health() -> None:
    with build_client() as client:
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json()["ok"] is True
        assert response.json()["version"] == APP_VERSION


def test_api_health_alias() -> None:
    with build_client() as client:
        response = client.get("/api/health")
        assert response.status_code == 200
        assert response.json()["ok"] is True
        assert response.headers["cache-control"] == "no-store, max-age=0"


def test_liveness_and_readiness_are_separate() -> None:
    client = build_client()
    client.app.state.audio_settings = SimpleNamespace(
        enabled=True,
        resolved_storage_mode="supabase",
        configuration_error="",
    )
    client.app.state.audio_store = SimpleNamespace(storage_ready=True)

    with client:
        live = client.get("/health/live")
        ready = client.get("/health/ready")

    assert live.status_code == 200
    assert live.json()["status"] == "alive"
    assert ready.status_code == 200
    assert ready.json()["status"] == "ready"
    assert ready.json()["checks"]["supabase"] == {"ok": True, "required": True}
    assert ready.json()["checks"]["visitEncryption"] == {
        "ok": True,
        "required": True,
    }


def test_readiness_returns_503_when_required_audio_storage_is_unavailable() -> None:
    client = build_client()
    client.app.state.audio_settings = SimpleNamespace(
        enabled=True,
        resolved_storage_mode="supabase",
        configuration_error="storage key missing",
    )
    client.app.state.audio_store = SimpleNamespace(storage_ready=False)

    with client:
        response = client.get("/health/ready")

    assert response.status_code == 503
    assert response.json()["status"] == "not_ready"
    assert response.json()["checks"]["audioStorage"] == {
        "ok": False,
        "required": True,
    }


def test_public_key_endpoint() -> None:
    with build_client() as client:
        response = client.get("/api/website/public-key")
        assert response.status_code == 200
        body = response.json()
        assert body["algorithm"] == ENCRYPTION_NAME
        assert len(base64.b64decode(body["publicKey"])) > 200


def test_v1_routes_are_available_without_removing_legacy_routes() -> None:
    with build_client() as client:
        legacy = client.get("/api/supporters")
        versioned = client.get("/api/v1/supporters")
        public_key = client.get("/api/v1/website/public-key")

    assert legacy.status_code == 200
    assert versioned.status_code == 200
    assert versioned.json()["supporters"] == legacy.json()["supporters"]
    assert public_key.status_code == 200


def test_plain_visit_is_rejected() -> None:
    with build_client() as client:
        response = client.post("/api/website/visit", json={"url": "http://localhost:3000/"})
        assert response.status_code == 400


def test_encrypted_visit_alert_and_duplicate() -> None:
    payload = {
        "eventId": "visit-1",
        "url": "http://localhost:3000/",
        "device": "Windows PC",
        "browser": "Chrome",
        "platform": "Windows 10/11",
        "userAgent": "Test Browser",
    }
    headers = {"x-forwarded-for": "203.0.113.10"}
    with build_client() as client:
        public_key = client.get("/api/website/public-key").json()["publicKey"]
        envelope = encrypt_for_backend(public_key, payload)
        first = client.post("/api/website/visit", headers=headers, json=envelope)
        second = client.post("/api/website/visit", headers=headers, json=envelope)
        assert first.status_code == 200
        assert first.json()["sent"] is True
        assert second.status_code == 200
        assert second.json()["duplicate"] is True


def test_tampered_ciphertext_is_rejected() -> None:
    with build_client() as client:
        public_key = client.get("/api/website/public-key").json()["publicKey"]
        envelope = encrypt_for_backend(public_key, {"url": "http://localhost:3000/"})
        raw = bytearray(base64.b64decode(envelope["ciphertext"]))
        raw[-1] ^= 1
        envelope["ciphertext"] = base64.b64encode(raw).decode("ascii")
        response = client.post("/api/website/visit", json=envelope)
        assert response.status_code == 400


def test_list_supporters_still_available_for_admin_use() -> None:
    with build_client() as client:
        response = client.get("/api/supporters")
        assert response.status_code == 200
        assert response.json()["supporters"][0]["name"] == "Sokha"
        assert response.json()["source"] == "memory-cache"
        assert response.json()["stale"] is False
        assert response.headers["x-supporters-source"] == "memory-cache"


def test_supporter_list_uses_signed_keyset_cursor() -> None:
    class FakePagedSupabase(FakeSupabase):
        def __init__(self) -> None:
            super().__init__()
            self.supporters = [
                {
                    "id": "00000000-0000-0000-0000-000000000003",
                    "name": "First",
                    "amount": "30.00",
                    "currency": "USD",
                    "created_at": "2026-07-25T03:00:00Z",
                },
                {
                    "id": "00000000-0000-0000-0000-000000000002",
                    "name": "Second",
                    "amount": "20.00",
                    "currency": "USD",
                    "created_at": "2026-07-25T02:00:00Z",
                },
                {
                    "id": "00000000-0000-0000-0000-000000000001",
                    "name": "Third",
                    "amount": "10.00",
                    "currency": "USD",
                    "created_at": "2026-07-25T01:00:00Z",
                },
            ]

        async def list_supporters_page(self, limit, cursor):
            ids = [row["id"] for row in self.supporters]
            start = ids.index(str(cursor.supporter_id)) + 1
            return self.supporters[start : start + limit]

    with build_client(FakePagedSupabase()) as client:
        first = client.get("/api/v1/supporters", params={"limit": 2})
        cursor = first.json()["nextCursor"]
        second = client.get(
            "/api/v1/supporters",
            params={"limit": 2, "cursor": cursor},
        )
        tampered = client.get(
            "/api/v1/supporters",
            params={"limit": 2, "cursor": f"{cursor}x"},
        )

    assert first.status_code == 200
    assert [row["name"] for row in first.json()["supporters"]] == ["First", "Second"]
    assert first.json()["hasMore"] is True
    assert cursor
    assert second.status_code == 200
    assert [row["name"] for row in second.json()["supporters"]] == ["Third"]
    assert second.json()["hasMore"] is False
    assert second.json()["nextCursor"] is None
    assert tampered.status_code == 400
    assert tampered.headers["content-type"].startswith("application/problem+json")
    assert tampered.json()["errorCode"] == "invalid_supporter_cursor"


def test_validation_errors_use_rfc_9457_problem_details() -> None:
    with build_client() as client:
        response = client.get("/api/v1/supporters", params={"limit": 0})

    body = response.json()
    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/problem+json")
    assert body["type"] == "urn:ozo-api:problem:validation_error"
    assert body["status"] == response.status_code
    assert body["errorCode"] == "validation_error"
    assert body["requestId"] == response.headers["x-request-id"]
    assert body["errors"][0]["pointer"] == "/limit"


def test_stale_supporters_are_returned_without_breaking_public_list() -> None:
    class FakeStaleSupabase(FakeSupabase):
        async def list_supporters_resilient(self, limit: int):
            return self.supporters[:limit], "stale-memory-cache", True

    with build_client(FakeStaleSupabase()) as client:
        response = client.get("/api/supporters")
        assert response.status_code == 200
        assert response.json()["stale"] is True
        assert response.headers["x-supporters-source"] == "stale-memory-cache"
        assert "Response is stale" in response.headers["warning"]


def test_create_requires_admin_key() -> None:
    with build_client() as client:
        response = client.post("/api/supporters", json={"name": "Dara", "amount": 10})
        assert response.status_code == 401


def test_create_supporter() -> None:
    with build_client() as client:
        response = client.post(
            "/api/supporters",
            headers={"X-Admin-Key": "test-admin-key-that-is-long-enough-123456"},
            json={"name": "Dara", "amount": 10, "currency": "usd"},
        )
        assert response.status_code == 201
        assert response.json()["supporter"]["currency"] == "USD"


def test_health_endpoint_is_keepalive_ready() -> None:
    with build_client() as client:
        response = client.get("/health")
        assert response.status_code == 200
        body = response.json()
        assert body["ok"] is True
        assert body["service"]
        assert body["visitEncryptionConfigured"] is True
        assert isinstance(body["uptimeSeconds"], (int, float))
        assert response.headers["cache-control"] == "no-store, max-age=0"
