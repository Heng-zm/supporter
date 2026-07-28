from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
import wave

import httpx

from app.audio_extension.audio_validation import detect_audio_mime
from app.audio_extension.config import AudioSettings
from app.audio_extension.store import AudioStore
from app.audio_extension.telegram import TelegramAudioController


def make_wav() -> bytes:
    import io

    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(8_000)
        wav.writeframes(b"\x00\x00" * 800)
    return output.getvalue()


def settings(tmp_path: Path) -> AudioSettings:
    return AudioSettings(
        enabled=True,
        storage_mode="local",
        storage_bucket="website-audio",
        storage_manifest_path="current.json",
        local_storage_directory=tmp_path,
        max_bytes=20_000_000,
        metadata_cache_seconds=0,
        pending_ttl_seconds=600,
        http_timeout_seconds=60,
        supabase_url="",
        supabase_secret_key="",
        telegram_bot_token="token",
        telegram_chat_id="100",
        telegram_admin_user_ids=frozenset({200}),
        telegram_allow_owner_private_chat=True,
        app_environment="test",
        require_persistent_storage=False,
    )


def test_local_store_round_trip(tmp_path: Path) -> None:
    async def run() -> None:
        async with httpx.AsyncClient() as client:
            store = AudioStore(settings(tmp_path), client)
            source = make_wav()
            metadata = await store.replace(
                file_name="track.wav",
                mime_type="audio/wav",
                data=source,
                uploaded_by=200,
                telegram_file_id="abc",
                telegram_update_id=1,
            )
            loaded_metadata, loaded = await store.get_audio(
                requested_version=metadata.version
            )
            assert loaded == source
            assert loaded_metadata.sha256 == metadata.sha256
            assert loaded_metadata.mime_type == "audio/wav"
            assert (tmp_path / "current.json").is_file()

    asyncio.run(run())


def test_audio_signature_detection() -> None:
    assert detect_audio_mime(make_wav()) == "audio/wav"
    assert detect_audio_mime(b"not audio") == ""


def test_telegram_authorization_and_media_extraction(tmp_path: Path) -> None:
    async def run() -> None:
        async with httpx.AsyncClient() as client:
            cfg = settings(tmp_path)
            store = AudioStore(cfg, client)
            controller = TelegramAudioController(cfg, client, store)
            message = {
                "chat": {"id": 100, "type": "group"},
                "from": {"id": 200},
                "audio": {
                    "file_id": "file-1",
                    "file_name": "music.mp3",
                    "mime_type": "audio/mpeg",
                    "file_size": 123,
                },
            }
            assert controller._authorized(message) is True
            media = controller._extract_media(message)
            assert media is not None
            assert media.file_name == "music.mp3"
            assert media.mime_type == "audio/mpeg"

            denied = {**message, "from": {"id": 999}}
            assert controller._authorized(denied) is False

    asyncio.run(run())


def test_supabase_storage_round_trip(tmp_path: Path) -> None:
    async def run() -> None:
        objects: dict[str, bytes] = {}
        write_order: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            bucket_url = "https://project.supabase.co/storage/v1/bucket/website-audio"
            if url == bucket_url and request.method == "GET":
                return httpx.Response(200, json={"id": "website-audio"})

            prefix = "https://project.supabase.co/storage/v1/object/website-audio/"
            assert url.startswith(prefix)
            path = url[len(prefix):]
            if request.method == "POST":
                objects[path] = request.content
                write_order.append(path)
                return httpx.Response(200, json={"path": path})
            if request.method == "GET":
                if path not in objects:
                    return httpx.Response(400, json={
                        "statusCode": "404",
                        "error": "not_found",
                        "message": "Object not found",
                    })
                return httpx.Response(200, content=objects[path])
            return httpx.Response(405)

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            cfg = replace(
                settings(tmp_path),
                storage_mode="supabase",
                supabase_url="https://project.supabase.co",
                supabase_secret_key="service-key",
            )
            writer = AudioStore(cfg, client)
            source = make_wav()
            metadata = await writer.replace(
                file_name="remote.wav",
                mime_type="audio/wav",
                data=source,
                uploaded_by=200,
                telegram_file_id="remote-1",
                telegram_update_id=2,
            )
            assert write_order[-1] == "current.json"
            assert write_order[0] == metadata.object_path

            reader = AudioStore(cfg, client)
            loaded_metadata, loaded = await reader.get_audio(
                requested_version=metadata.version
            )
            assert loaded == source
            assert loaded_metadata.version == metadata.version

    asyncio.run(run())


def test_telegram_audio_command_replaces_track(tmp_path: Path) -> None:
    async def run() -> None:
        source = make_wav()
        sent_messages: list[str] = []
        get_file_calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal get_file_calls
            url = str(request.url)
            if url.endswith("/getFile"):
                get_file_calls += 1
                return httpx.Response(
                    200,
                    json={
                        "ok": True,
                        "result": {
                            "file_path": "audio/upload.wav",
                            "file_size": len(source),
                        },
                    },
                )
            if "/file/bottoken/audio/upload.wav" in url:
                return httpx.Response(200, content=source)
            if url.endswith("/sendMessage"):
                payload = __import__("json").loads(request.content)
                sent_messages.append(payload["text"])
                return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})
            return httpx.Response(404)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            cfg = settings(tmp_path)
            store = AudioStore(cfg, client)
            controller = TelegramAudioController(cfg, client, store)
            update = {
                "update_id": 555,
                "message": {
                    "chat": {"id": 100, "type": "group"},
                    "from": {"id": 200},
                    "caption": "/audio",
                    "audio": {
                        "file_id": "telegram-file",
                        "file_name": "upload.wav",
                        "mime_type": "audio/wav",
                        "file_size": len(source),
                    },
                },
            }
            handled = await controller.handle_update(update)
            replay_handled = await controller.handle_update(update)
            assert handled is True
            assert replay_handled is True
            assert get_file_calls == 1
            metadata, loaded = await store.get_audio()
            assert loaded == source
            assert metadata.file_name == "upload.wav"
            assert any("updated successfully" in text for text in sent_messages)

    asyncio.run(run())


def test_public_audio_router(tmp_path: Path) -> None:
    from fastapi import FastAPI
    from app.audio_extension.router import router

    async def run() -> None:
        source = make_wav()
        async with httpx.AsyncClient() as storage_client:
            store = AudioStore(settings(tmp_path), storage_client)
            metadata = await store.replace(
                file_name="api.wav",
                mime_type="audio/wav",
                data=source,
                uploaded_by=200,
                telegram_file_id=None,
                telegram_update_id=None,
            )

            app = FastAPI()
            app.state.audio_store = store
            app.include_router(router, prefix="/api")
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://test",
            ) as client:
                meta_response = await client.get("/api/audio/metadata")
                assert meta_response.status_code == 200
                assert meta_response.json()["version"] == metadata.version

                file_response = await client.get(
                    "/api/audio/file",
                    params={"version": metadata.version},
                )
                assert file_response.status_code == 200
                assert file_response.content == source
                assert file_response.headers["x-audio-version"] == metadata.version

                stale_response = await client.get(
                    "/api/audio/file",
                    params={"version": "old-version"},
                )
                assert stale_response.status_code == 409

    asyncio.run(run())


def test_python_telegram_bot_adapter_registration(tmp_path: Path, monkeypatch) -> None:
    import sys
    import types

    from app.audio_extension.ptb_adapter import register_python_telegram_bot_handler

    class FakeHandlerStop(Exception):
        pass

    class FakeMessageHandler:
        def __init__(self, message_filter, callback):
            self.message_filter = message_filter
            self.callback = callback

    fake_ext = types.ModuleType("telegram.ext")
    fake_ext.ApplicationHandlerStop = FakeHandlerStop
    fake_ext.MessageHandler = FakeMessageHandler
    fake_ext.filters = types.SimpleNamespace(ALL=object())
    fake_telegram = types.ModuleType("telegram")
    fake_telegram.ext = fake_ext
    monkeypatch.setitem(sys.modules, "telegram", fake_telegram)
    monkeypatch.setitem(sys.modules, "telegram.ext", fake_ext)

    class FakeApplication:
        def __init__(self):
            self.bot_data = {}
            self.handlers = []

        def add_handler(self, handler, group=0):
            self.handlers.append((handler, group))

    async def create_controller():
        client = httpx.AsyncClient()
        controller = TelegramAudioController(
            settings(tmp_path),
            client,
            AudioStore(settings(tmp_path), client),
        )
        return client, controller

    client, controller = asyncio.run(create_controller())
    try:
        application = FakeApplication()
        register_python_telegram_bot_handler(
            application,
            controller,
            group=-90,
        )
        register_python_telegram_bot_handler(
            application,
            controller,
            group=-90,
        )
        assert len(application.handlers) == 1
        assert application.handlers[0][1] == -90
    finally:
        asyncio.run(client.aclose())


def test_settings_accept_service_role_alias(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("APP_ENVIRONMENT", "production")
    monkeypatch.setenv("SUPABASE_URL", "https://project.supabase.co")
    monkeypatch.delenv("SUPABASE_SECRET_KEY", raising=False)
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "service-role")
    cfg = AudioSettings.from_env()
    assert cfg.supabase_secret_key == "service-role"
    assert cfg.resolved_storage_mode == "supabase"
    assert cfg.configuration_error == ""


def test_settings_accept_supabase_key_alias(monkeypatch) -> None:
    monkeypatch.setenv("APP_ENVIRONMENT", "production")
    monkeypatch.setenv("SUPABASE_URL", "https://project.supabase.co")
    monkeypatch.delenv("SUPABASE_SECRET_KEY", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)
    monkeypatch.setenv("SUPABASE_KEY", "existing-service-key")

    cfg = AudioSettings.from_env()

    assert cfg.supabase_secret_key == "existing-service-key"
    assert cfg.resolved_storage_mode == "supabase"
    assert cfg.configuration_error == ""


def test_supabase_missing_manifest_http_400_is_available_false(tmp_path: Path) -> None:
    async def run() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if url.endswith("/storage/v1/bucket/website-audio"):
                return httpx.Response(200, json={"id": "website-audio"})
            if url.endswith("/storage/v1/object/website-audio/current.json"):
                return httpx.Response(
                    400,
                    json={
                        "statusCode": "404",
                        "error": "not_found",
                        "message": "Object not found",
                    },
                )
            return httpx.Response(404)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            cfg = replace(
                settings(tmp_path),
                storage_mode="supabase",
                supabase_url="https://project.supabase.co",
                supabase_secret_key="service-key",
            )
            store = AudioStore(cfg, client)
            assert await store.get_metadata() is None
            assert store.storage_ready is True

    asyncio.run(run())


def test_supabase_bucket_is_created_automatically(tmp_path: Path) -> None:
    async def run() -> None:
        bucket_exists = False
        create_payload: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal bucket_exists, create_payload
            url = str(request.url)
            if url.endswith("/storage/v1/bucket/website-audio") and request.method == "GET":
                if bucket_exists:
                    return httpx.Response(200, json={"id": "website-audio"})
                return httpx.Response(
                    400,
                    json={
                        "statusCode": "404",
                        "error": "not_found",
                        "message": "Bucket not found",
                    },
                )
            if url.endswith("/storage/v1/bucket") and request.method == "POST":
                create_payload = __import__("json").loads(request.content)
                bucket_exists = True
                return httpx.Response(200, json={"name": "website-audio"})
            return httpx.Response(404)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            cfg = replace(
                settings(tmp_path),
                storage_mode="supabase",
                supabase_url="https://project.supabase.co",
                supabase_secret_key="service-key",
                auto_create_bucket=True,
            )
            store = AudioStore(cfg, client)
            await store.initialize()
            assert store.storage_ready is True
            assert create_payload["id"] == "website-audio"
            assert create_payload["public"] is False
            assert create_payload["file_size_limit"] == 20_000_000

    asyncio.run(run())


def test_supabase_auth_error_has_safe_code(tmp_path: Path) -> None:
    async def run() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(403, json={"message": "Invalid API key"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            cfg = replace(
                settings(tmp_path),
                storage_mode="supabase",
                supabase_url="https://project.supabase.co",
                supabase_secret_key="wrong-key",
            )
            store = AudioStore(cfg, client)
            try:
                await store.get_metadata()
            except Exception as exc:
                assert getattr(exc, "code", "") == "supabase_auth_failed"
            else:
                raise AssertionError("Expected Supabase authentication failure")

    asyncio.run(run())



def test_audio_settings_are_stored_in_source(monkeypatch) -> None:
    # AUDIO_* environment entries must not alter the source-controlled values.
    monkeypatch.setenv("AUDIO_ENABLED", "false")
    monkeypatch.setenv("AUDIO_STORAGE_MODE", "local")
    monkeypatch.setenv("AUDIO_STORAGE_BUCKET", "wrong-bucket")
    monkeypatch.setenv("AUDIO_STORAGE_MANIFEST_PATH", "wrong.json")
    monkeypatch.setenv("AUDIO_MAX_BYTES", "1024")
    monkeypatch.setenv("AUDIO_METADATA_CACHE_SECONDS", "99")
    monkeypatch.setenv("AUDIO_PENDING_TTL_SECONDS", "99")
    monkeypatch.setenv("AUDIO_HTTP_TIMEOUT_SECONDS", "99")
    monkeypatch.setenv("AUDIO_REQUIRE_PERSISTENT_STORAGE", "false")
    monkeypatch.setenv("AUDIO_TELEGRAM_ALLOW_OWNER_PRIVATE_CHAT", "false")
    monkeypatch.setenv("AUDIO_LOCAL_STORAGE_DIRECTORY", "/tmp/wrong-audio-path")

    cfg = AudioSettings.from_env()

    assert cfg.enabled is True
    assert cfg.storage_mode == "auto"
    assert cfg.storage_bucket == "website-audio"
    assert cfg.storage_manifest_path == "current.json"
    assert cfg.local_storage_directory == Path("data/website-audio")
    assert cfg.max_bytes == 20_000_000
    assert cfg.metadata_cache_seconds == 5
    assert cfg.pending_ttl_seconds == 600
    assert cfg.http_timeout_seconds == 60
    assert cfg.require_persistent_storage is True
    assert cfg.auto_create_bucket is True
    assert cfg.telegram_allow_owner_private_chat is True



def test_source_controlled_cors_origins(monkeypatch) -> None:
    from app.audio_extension.cors import get_backend_cors_origins

    monkeypatch.setenv(
        "BACKEND_CORS_ORIGINS",
        "https://wrong.example,https://also-wrong.example",
    )

    assert get_backend_cors_origins() == (
        "https://pay-coffee-topaz.vercel.app",
        "https://j-s-ng-o-rgn-sz-lrgkldgs.vercel.app",
    )


def test_cors_preflight_allows_both_frontends() -> None:
    from fastapi import FastAPI
    from fastapi.middleware.cors import CORSMiddleware

    from app.audio_extension.cors import get_backend_cors_origins

    async def run() -> None:
        app = FastAPI()
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(get_backend_cors_origins()),
            allow_credentials=False,
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["Accept", "Content-Type"],
        )

        @app.get("/health")
        async def health() -> dict[str, bool]:
            return {"ok": True}

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            for origin in get_backend_cors_origins():
                response = await client.options(
                    "/health",
                    headers={
                        "Origin": origin,
                        "Access-Control-Request-Method": "GET",
                    },
                )
                assert response.status_code == 200
                assert response.headers["access-control-allow-origin"] == origin

            denied = await client.options(
                "/health",
                headers={
                    "Origin": "https://not-allowed.example",
                    "Access-Control-Request-Method": "GET",
                },
            )
            assert "access-control-allow-origin" not in denied.headers

    asyncio.run(run())


def test_packaged_main_mounts_audio_router_and_lifespan() -> None:
    from pathlib import Path

    main_path = Path(__file__).resolve().parents[1] / "app" / "main.py"
    source = main_path.read_text(encoding="utf-8")

    assert "include_audio_router(app, api_prefix=runtime_settings.api_prefix)" in source
    assert "await start_audio_extension(app)" in source
    assert "await close_audio_extension(app)" in source
    assert '"/api/audio/metadata"' not in source  # route prefix stays configuration-driven


def test_audio_webhook_rejects_invalid_secret(tmp_path: Path) -> None:
    from fastapi import FastAPI

    from app.audio_extension.webhook import (
        AudioTelegramWebhookSettings,
        include_audio_telegram_webhook_router,
    )

    async def run() -> None:
        app = FastAPI()
        app.state.audio_api_prefix = "/api"
        app.state.audio_telegram_webhook_settings = AudioTelegramWebhookSettings(
            bot_token="token",
            secret_token="correct_secret",
            webhook_url="https://example.com/api/telegram/webhook",
            auto_configure=True,
            drop_pending_updates=False,
            max_connections=10,
        )
        include_audio_telegram_webhook_router(app, api_prefix="/api")

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.post(
                "/api/telegram/webhook",
                headers={
                    "Content-Type": "application/json",
                    "X-Telegram-Bot-Api-Secret-Token": "wrong_secret",
                },
                json={"update_id": 1, "message": {}},
            )
            assert response.status_code == 403

    asyncio.run(run())


def test_audio_webhook_dispatches_audio_status(tmp_path: Path) -> None:
    from fastapi import FastAPI

    from app.audio_extension.webhook import (
        AudioTelegramWebhookSettings,
        include_audio_telegram_webhook_router,
    )

    async def run() -> None:
        sent_messages: list[dict[str, object]] = []

        def telegram_handler(request: httpx.Request) -> httpx.Response:
            if str(request.url).endswith("/sendMessage"):
                sent_messages.append(__import__("json").loads(request.content))
                return httpx.Response(
                    200,
                    json={"ok": True, "result": {"message_id": 1}},
                )
            return httpx.Response(404)

        telegram_client = httpx.AsyncClient(
            transport=httpx.MockTransport(telegram_handler)
        )
        try:
            cfg = settings(tmp_path)
            store = AudioStore(cfg, telegram_client)
            controller = TelegramAudioController(cfg, telegram_client, store)

            app = FastAPI()
            app.state.audio_api_prefix = "/api"
            app.state.audio_telegram = controller
            app.state.audio_telegram_webhook_settings = AudioTelegramWebhookSettings(
                bot_token="token",
                secret_token="correct_secret",
                webhook_url="https://example.com/api/telegram/webhook",
                auto_configure=True,
                drop_pending_updates=False,
                max_connections=10,
            )
            include_audio_telegram_webhook_router(app, api_prefix="/api")

            update = {
                "update_id": 101,
                "message": {
                    "message_id": 5,
                    "text": "/audio status",
                    "chat": {"id": 100, "type": "group"},
                    "from": {"id": 200, "is_bot": False},
                },
            }

            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                response = await client.post(
                    "/api/telegram/webhook",
                    headers={
                        "Content-Type": "application/json",
                        "X-Telegram-Bot-Api-Secret-Token": "correct_secret",
                    },
                    json=update,
                )
                assert response.status_code == 200
                assert response.json() == {"ok": True, "handled": True}

                duplicate = await client.post(
                    "/api/telegram/webhook",
                    headers={
                        "Content-Type": "application/json",
                        "X-Telegram-Bot-Api-Secret-Token": "correct_secret",
                    },
                    json=update,
                )
                assert duplicate.status_code == 200
                assert duplicate.json()["duplicate"] is True

            assert len(sent_messages) == 1
            assert sent_messages[0]["chat_id"] == "100"
            assert "No Telegram-managed website audio" in str(
                sent_messages[0]["text"]
            )
        finally:
            await telegram_client.aclose()

    asyncio.run(run())


def test_configure_audio_telegram_webhook_and_verify(monkeypatch) -> None:
    from fastapi import FastAPI

    from app.audio_extension.webhook import configure_audio_telegram_webhook

    async def run() -> None:
        calls: list[tuple[str, dict[str, object]]] = []
        expected_url = "https://supporter-ipio.onrender.com/api/telegram/webhook"

        def handler(request: httpx.Request) -> httpx.Response:
            payload = __import__("json").loads(request.content or b"{}")
            calls.append((str(request.url), payload))
            if str(request.url).endswith("/setWebhook"):
                return httpx.Response(200, json={"ok": True, "result": True})
            if str(request.url).endswith("/getWebhookInfo"):
                return httpx.Response(
                    200,
                    json={
                        "ok": True,
                        "result": {
                            "url": expected_url,
                            "pending_update_count": 0,
                        },
                    },
                )
            return httpx.Response(404)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            app = FastAPI()
            app.state.audio_http_client = client
            configured = await configure_audio_telegram_webhook(
                app,
                api_prefix="/api",
            )
            assert configured is True
            assert app.state.audio_telegram_webhook_configured is True
            assert app.state.audio_telegram_webhook_error == ""
            assert len(calls) == 2
            set_payload = calls[0][1]
            assert set_payload["url"] == expected_url
            assert set_payload["secret_token"] == "safe_secret_123"
            assert set_payload["allowed_updates"] == ["message"]
            assert set_payload["drop_pending_updates"] is False
            assert set_payload["max_connections"] == 10
        finally:
            await client.aclose()

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:test-token")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "safe_secret_123")
    monkeypatch.setenv(
        "TELEGRAM_WEBHOOK_URL",
        "https://supporter-ipio.onrender.com/api/telegram/webhook",
    )
    monkeypatch.setenv("TELEGRAM_AUTO_CONFIGURE_WEBHOOK", "true")
    asyncio.run(run())


def test_webhook_settings_derive_render_url(monkeypatch) -> None:
    from app.audio_extension.webhook import AudioTelegramWebhookSettings

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:test-token")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "safe_secret")
    monkeypatch.delenv("TELEGRAM_WEBHOOK_URL", raising=False)
    monkeypatch.setenv("RENDER_EXTERNAL_URL", "https://supporter-ipio.onrender.com/")

    cfg = AudioTelegramWebhookSettings.from_env(api_prefix="/api")

    assert cfg.webhook_url == (
        "https://supporter-ipio.onrender.com/api/telegram/webhook"
    )
    assert cfg.configuration_error == ""


def test_packaged_main_mounts_telegram_webhook() -> None:
    main_path = Path(__file__).resolve().parents[1] / "app" / "main.py"
    source = main_path.read_text(encoding="utf-8")

    assert "include_audio_telegram_webhook_router(" in source
    assert "await configure_audio_telegram_webhook(" in source
    assert '"audioTelegramWebhookConfigured"' in source
