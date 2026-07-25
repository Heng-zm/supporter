from __future__ import annotations

import base64
import json
from contextlib import asynccontextmanager
from uuid import uuid4

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
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
    settings = Settings(
        app_environment="test",
        supporters_admin_key="test-admin-key",
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
        assert response.json()["version"] == "1.1.0"


def test_api_health_alias() -> None:
    with build_client() as client:
        response = client.get("/api/health")
        assert response.status_code == 200
        assert response.json()["ok"] is True
        assert response.headers["cache-control"] == "no-store, max-age=0"


def test_public_key_endpoint() -> None:
    with build_client() as client:
        response = client.get("/api/website/public-key")
        assert response.status_code == 200
        body = response.json()
        assert body["algorithm"] == ENCRYPTION_NAME
        assert len(base64.b64decode(body["publicKey"])) > 200


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
            headers={"X-Admin-Key": "test-admin-key"},
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
