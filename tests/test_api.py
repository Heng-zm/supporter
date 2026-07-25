from __future__ import annotations

from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.services.telegram import TelegramResult


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


def build_client() -> TestClient:
    settings = Settings(
        app_environment="test",
        supporters_admin_key="test-admin-key",
        visit_hash_salt="test-hash-salt",
        backend_cors_origins_raw="http://localhost:3000",
    )
    app = create_app(settings)
    app.router.lifespan_context = noop_lifespan
    app.state.settings = settings
    app.state.supabase = FakeSupabase()
    app.state.telegram = FakeTelegram()
    from app.services.visits import VisitService

    app.state.visits = VisitService(settings, app.state.supabase, app.state.telegram)
    return TestClient(app)


def test_health() -> None:
    with build_client() as client:
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json()["ok"] is True


def test_list_supporters() -> None:
    with build_client() as client:
        response = client.get("/api/supporters")
        assert response.status_code == 200
        assert response.json()["supporters"][0]["name"] == "Sokha"


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


def test_visit_alert_and_duplicate() -> None:
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
        first = client.post("/api/website/visit", headers=headers, json=payload)
        second = client.post("/api/website/visit", headers=headers, json=payload)
        assert first.status_code == 200
        assert first.json()["sent"] is True
        assert second.status_code == 200
        assert second.json()["duplicate"] is True
