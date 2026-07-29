from __future__ import annotations

import asyncio
import wave
from dataclasses import replace
from pathlib import Path

import httpx

from app.audio_extension.audio_validation import detect_audio_mime
from app.audio_extension.config import AudioSettings
from app.audio_extension.store import AudioStore
from app.audio_extension.telegram import TelegramAudioController
from app.config import Settings


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
        history_manifest_path="history.json",
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
        encryption_enabled=True,
        encryption_algorithm="AES-256-GCM-CHUNKED",
        encryption_active_key_version="v1",
        encryption_chunk_bytes=64 * 1024,
        encryption_keys={"v1": b"K" * 32},
        history_limit=10,
        range_requests_enabled=True,
        response_chunk_bytes=64 * 1024,
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
            assert create_payload["file_size_limit"] == cfg.max_ciphertext_bytes
            assert "application/octet-stream" in create_payload["allowed_mime_types"]

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
            app.state.settings = Settings(
                require_encrypted_visits=False,
                supabase_url="https://test.supabase.co",
                supabase_secret_key="server-secret",
                telegram_bot_token="123456:test-token",
                telegram_chat_id="100",
                telegram_commands_enabled=True,
                telegram_webhook_secret=(
                    "safe_secret_123456789012345"
                ),
                telegram_webhook_url=expected_url,
                telegram_auto_configure_webhook=True,
            )
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
            assert set_payload["secret_token"] == "safe_secret_123456789012345"
            assert set_payload["allowed_updates"] == ["message", "callback_query"]
            assert set_payload["drop_pending_updates"] is False
            assert set_payload["max_connections"] == 10
        finally:
            await client.aclose()

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:test-token")
    monkeypatch.setenv(
        "TELEGRAM_WEBHOOK_SECRET",
        "safe_secret_123456789012345",
    )
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


def test_webhook_auto_configuration_is_opt_in(monkeypatch) -> None:
    from app.audio_extension.webhook import AudioTelegramWebhookSettings

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:test-token")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "safe_secret")
    monkeypatch.setenv(
        "TELEGRAM_WEBHOOK_URL",
        "https://supporter-ipio.onrender.com/api/telegram/webhook",
    )
    monkeypatch.delenv("TELEGRAM_AUTO_CONFIGURE_WEBHOOK", raising=False)

    cfg = AudioTelegramWebhookSettings.from_env(api_prefix="/api")

    assert cfg.configuration_error == ""
    assert cfg.auto_configure is False


def test_packaged_main_mounts_telegram_webhook() -> None:
    main_path = Path(__file__).resolve().parents[1] / "app" / "main.py"
    source = main_path.read_text(encoding="utf-8")

    assert "include_audio_telegram_webhook_router(" in source
    assert "await configure_audio_telegram_webhook(" in source
    assert '"audioTelegramWebhookConfigured"' in source


def test_private_chat_allows_listed_admin_when_group_is_configured(tmp_path: Path) -> None:
    async def run() -> None:
        async with httpx.AsyncClient() as client:
            cfg = settings(tmp_path)
            controller = TelegramAudioController(cfg, client, AudioStore(cfg, client))

            admin_private_message = {
                "chat": {"id": 200, "type": "private"},
                "from": {"id": 200},
            }
            non_admin_private_message = {
                "chat": {"id": 999, "type": "private"},
                "from": {"id": 999},
            }

            assert controller._authorized(admin_private_message) is True
            assert controller._authorized(non_admin_private_message) is False

    asyncio.run(run())


def test_missing_manifest_is_cached(tmp_path: Path) -> None:
    async def run() -> None:
        manifest_reads = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal manifest_reads
            url = str(request.url)
            if url.endswith("/storage/v1/bucket/website-audio"):
                return httpx.Response(200, json={"id": "website-audio"})
            if url.endswith("/storage/v1/object/website-audio/current.json"):
                manifest_reads += 1
                return httpx.Response(
                    400,
                    json={
                        "statusCode": "404",
                        "error": "not_found",
                        "message": "Object not found",
                    },
                )
            return httpx.Response(404)

        cfg = replace(
            settings(tmp_path),
            storage_mode="supabase",
            supabase_url="https://project.supabase.co",
            supabase_secret_key="service-key",
            metadata_cache_seconds=30,
        )
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            store = AudioStore(cfg, client)
            assert await store.get_metadata() is None
            assert await store.get_metadata() is None
            assert manifest_reads == 1

    asyncio.run(run())


def test_storage_failure_uses_short_backoff(tmp_path: Path) -> None:
    async def run() -> None:
        bucket_checks = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal bucket_checks
            if str(request.url).endswith("/storage/v1/bucket/website-audio"):
                bucket_checks += 1
                return httpx.Response(500, json={"message": "temporary failure"})
            return httpx.Response(404)

        cfg = replace(
            settings(tmp_path),
            storage_mode="supabase",
            supabase_url="https://project.supabase.co",
            supabase_secret_key="service-key",
        )
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            store = AudioStore(cfg, client)
            for _ in range(2):
                try:
                    await store.get_metadata()
                except Exception as exc:
                    assert getattr(exc, "code", "") == "supabase_storage_request_failed"
                else:
                    raise AssertionError("Storage request should have failed.")
            assert bucket_checks == 1

    asyncio.run(run())


def test_concurrent_duplicate_telegram_update_writes_once(tmp_path: Path) -> None:
    async def run() -> None:
        async with httpx.AsyncClient() as client:
            store = AudioStore(settings(tmp_path), client)
            source = make_wav()

            first, second = await asyncio.gather(
                store.replace(
                    file_name="same.wav",
                    mime_type="audio/wav",
                    data=source,
                    uploaded_by=200,
                    telegram_file_id="same-file",
                    telegram_update_id=777,
                ),
                store.replace(
                    file_name="same.wav",
                    mime_type="audio/wav",
                    data=source,
                    uploaded_by=200,
                    telegram_file_id="same-file",
                    telegram_update_id=777,
                ),
            )

            assert first.version == second.version
            version_files = [
                path
                for path in (tmp_path / "versions").rglob("*")
                if path.is_file()
            ]
            assert len(version_files) == 1

    asyncio.run(run())


def test_failed_manifest_write_removes_orphaned_version(tmp_path: Path) -> None:
    from app.audio_extension.store import AudioStoreError

    async def run() -> None:
        blocker = tmp_path / "blocked"
        blocker.write_text("not a directory", encoding="utf-8")
        cfg = replace(
            settings(tmp_path),
            storage_manifest_path="blocked/current.json",
        )

        async with httpx.AsyncClient() as client:
            store = AudioStore(cfg, client)
            try:
                await store.replace(
                    file_name="orphan.wav",
                    mime_type="audio/wav",
                    data=make_wav(),
                    uploaded_by=200,
                    telegram_file_id="orphan",
                    telegram_update_id=None,
                )
            except AudioStoreError as exc:
                assert exc.code == "local_storage_write_failed"
            else:
                raise AssertionError("Manifest write should have failed.")

        versions = tmp_path / "versions"
        assert not versions.exists() or not any(path.is_file() for path in versions.rglob("*"))

    asyncio.run(run())


def test_telegram_download_rejects_oversized_stream(tmp_path: Path) -> None:
    async def run() -> None:
        payload = b"x" * 101

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if url.endswith("/getFile"):
                return httpx.Response(
                    200,
                    json={
                        "ok": True,
                        "result": {"file_path": "audio/file.bin"},
                    },
                )
            if "/file/bottoken/audio/file.bin" in url:
                return httpx.Response(200, content=payload)
            return httpx.Response(404)

        cfg = replace(settings(tmp_path), max_bytes=100)
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            controller = TelegramAudioController(cfg, client, AudioStore(cfg, client))
            media = controller._extract_media(
                {
                    "document": {
                        "file_id": "file-id",
                        "file_name": "file.mp3",
                        "mime_type": "audio/mpeg",
                    }
                }
            )
            assert media is not None
            try:
                await controller._download(media)
            except ValueError as exc:
                assert "too large" in str(exc).lower()
            else:
                raise AssertionError("Oversized Telegram download should be rejected.")

    asyncio.run(run())


def test_metadata_etag_returns_not_modified(tmp_path: Path) -> None:
    from fastapi import FastAPI

    from app.audio_extension.router import router

    async def run() -> None:
        async with httpx.AsyncClient() as storage_client:
            store = AudioStore(settings(tmp_path), storage_client)
            metadata = await store.replace(
                file_name="etag.wav",
                mime_type="audio/wav",
                data=make_wav(),
                uploaded_by=200,
                telegram_file_id=None,
                telegram_update_id=None,
            )

            app = FastAPI()
            app.state.audio_store = store
            app.include_router(router, prefix="/api")
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                response = await client.get(
                    "/api/audio/metadata",
                    headers={"If-None-Match": f'"{metadata.version}"'},
                )
                assert response.status_code == 304
                assert response.content == b""
                assert response.headers["etag"] == f'"{metadata.version}"'

    asyncio.run(run())


def test_anon_supabase_key_is_rejected_before_network(monkeypatch) -> None:
    import base64
    import json

    from app.audio_extension.config import AudioSettings

    def encode(value: dict[str, object]) -> str:
        raw = json.dumps(value, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    anon_key = f"{encode({'alg': 'HS256'})}.{encode({'role': 'anon'})}.signature"
    monkeypatch.setenv("SUPABASE_URL", "https://project.supabase.co")
    monkeypatch.setenv("SUPABASE_SECRET_KEY", anon_key)
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)
    monkeypatch.delenv("SUPABASE_KEY", raising=False)

    cfg = AudioSettings.from_env()
    assert 'role "anon"' in cfg.configuration_error


def test_encrypted_storage_contains_no_plaintext_and_validates_hashes(tmp_path: Path) -> None:
    async def run() -> None:
        source = make_wav() + b"\x01\x02\x03\x04" * 40_000
        cfg = settings(tmp_path)
        async with httpx.AsyncClient() as client:
            writer = AudioStore(cfg, client)
            metadata = await writer.replace(
                file_name="secret.wav",
                mime_type="audio/wav",
                data=source,
                uploaded_by=200,
                telegram_file_id="encrypted-file",
                telegram_update_id=901,
            )

            assert metadata.encrypted is True
            assert metadata.encryption_algorithm == "AES-256-GCM-CHUNKED"
            assert metadata.encryption_key_version == "v1"
            assert metadata.ciphertext_byte_length is not None
            assert metadata.ciphertext_byte_length > metadata.byte_length
            assert metadata.ciphertext_sha256 is not None
            assert metadata.object_path.endswith(".agcm")

            encrypted_bytes = (tmp_path / metadata.object_path).read_bytes()
            assert encrypted_bytes.startswith(b"RAAEGCM1")
            assert not encrypted_bytes.startswith(b"RIFF")
            assert source[:128] not in encrypted_bytes
            assert __import__("hashlib").sha256(encrypted_bytes).hexdigest() == (
                metadata.ciphertext_sha256
            )

            # A new store instance cannot use the plaintext cache from replace().
            reader = AudioStore(cfg, client)
            loaded_metadata, loaded = await reader.get_audio(
                requested_version=metadata.version
            )
            assert loaded_metadata.sha256 == __import__("hashlib").sha256(source).hexdigest()
            assert loaded == source

    asyncio.run(run())


def test_encrypted_ciphertext_tampering_is_rejected(tmp_path: Path) -> None:
    from app.audio_extension.store import AudioStoreError

    async def run() -> None:
        source = make_wav() + b"A" * 100_000
        cfg = settings(tmp_path)
        async with httpx.AsyncClient() as client:
            writer = AudioStore(cfg, client)
            metadata = await writer.replace(
                file_name="tamper.wav",
                mime_type="audio/wav",
                data=source,
                uploaded_by=200,
                telegram_file_id=None,
                telegram_update_id=None,
            )
            path = tmp_path / metadata.object_path
            damaged = bytearray(path.read_bytes())
            damaged[-20] ^= 0x01
            path.write_bytes(damaged)

            reader = AudioStore(cfg, client)
            try:
                await reader.get_audio(requested_version=metadata.version)
            except AudioStoreError as exc:
                assert exc.code in {
                    "encrypted_audio_authentication_failed",
                    "encrypted_audio_checksum_mismatch",
                }
            else:
                raise AssertionError("Tampered encrypted audio should be rejected.")

    asyncio.run(run())


def test_per_file_nonce_produces_different_ciphertext(tmp_path: Path) -> None:
    async def run() -> None:
        source = make_wav() + b"B" * 80_000
        cfg = settings(tmp_path)
        async with httpx.AsyncClient() as client:
            store = AudioStore(cfg, client)
            first = await store.replace(
                file_name="same.wav",
                mime_type="audio/wav",
                data=source,
                uploaded_by=200,
                telegram_file_id="one",
                telegram_update_id=1001,
            )
            second = await store.replace(
                file_name="same.wav",
                mime_type="audio/wav",
                data=source,
                uploaded_by=200,
                telegram_file_id="two",
                telegram_update_id=1002,
            )
            first_bytes = (tmp_path / first.object_path).read_bytes()
            second_bytes = (tmp_path / second.object_path).read_bytes()
            assert first.sha256 == second.sha256
            assert first.ciphertext_sha256 != second.ciphertext_sha256
            assert first_bytes != second_bytes

    asyncio.run(run())


def test_key_rotation_reads_old_versions_and_encrypts_new_version(tmp_path: Path) -> None:
    from app.audio_extension.store import AudioStoreError

    async def run() -> None:
        source_v1 = make_wav() + b"V1" * 40_000
        source_v2 = make_wav() + b"V2" * 50_000
        cfg_v1 = settings(tmp_path)

        async with httpx.AsyncClient() as client:
            first_store = AudioStore(cfg_v1, client)
            first = await first_store.replace(
                file_name="v1.wav",
                mime_type="audio/wav",
                data=source_v1,
                uploaded_by=200,
                telegram_file_id=None,
                telegram_update_id=1101,
            )

            cfg_v2 = replace(
                cfg_v1,
                encryption_active_key_version="v2",
                encryption_keys={"v1": b"K" * 32, "v2": b"Z" * 32},
            )
            rotated_store = AudioStore(cfg_v2, client)
            old_metadata, old_audio = await rotated_store.get_audio(
                requested_version=first.version
            )
            assert old_metadata.encryption_key_version == "v1"
            assert old_audio == source_v1

            second = await rotated_store.replace(
                file_name="v2.wav",
                mime_type="audio/wav",
                data=source_v2,
                uploaded_by=200,
                telegram_file_id=None,
                telegram_update_id=1102,
            )
            assert second.encryption_key_version == "v2"

            rolled_back = await rotated_store.rollback("2")
            assert rolled_back.version == first.version
            _, restored = await rotated_store.get_audio()
            assert restored == source_v1

            cfg_without_old_key = replace(
                cfg_v2,
                encryption_keys={"v2": b"Z" * 32},
            )
            missing_key_store = AudioStore(cfg_without_old_key, client)
            try:
                await missing_key_store.get_audio()
            except AudioStoreError as exc:
                assert exc.code == "audio_decryption_key_unavailable"
            else:
                raise AssertionError("Old encrypted audio must require its old key.")

    asyncio.run(run())


def test_encrypted_history_and_telegram_rollback(tmp_path: Path) -> None:
    async def run() -> None:
        sent_messages: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url).endswith("/sendMessage"):
                payload = __import__("json").loads(request.content)
                sent_messages.append(payload["text"])
                return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})
            return httpx.Response(404)

        cfg = settings(tmp_path)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            store = AudioStore(cfg, client)
            first_source = make_wav() + b"1" * 70_000
            second_source = make_wav() + b"2" * 70_000
            first = await store.replace(
                file_name="first.wav",
                mime_type="audio/wav",
                data=first_source,
                uploaded_by=200,
                telegram_file_id=None,
                telegram_update_id=1201,
            )
            await store.replace(
                file_name="second.wav",
                mime_type="audio/wav",
                data=second_source,
                uploaded_by=200,
                telegram_file_id=None,
                telegram_update_id=1202,
            )

            controller = TelegramAudioController(cfg, client, store)
            history_update = {
                "update_id": 1203,
                "message": {
                    "chat": {"id": 100, "type": "group"},
                    "from": {"id": 200},
                    "text": "/audio history",
                },
            }
            rollback_update = {
                "update_id": 1204,
                "message": {
                    "chat": {"id": 100, "type": "group"},
                    "from": {"id": 200},
                    "text": "/audio rollback 2",
                },
            }
            assert await controller.handle_update(history_update) is True
            assert await controller.handle_update(rollback_update) is True
            active = await store.get_metadata(force=True)
            assert active is not None and active.version == first.version
            _, restored = await store.get_audio()
            assert restored == first_source
            assert any("audio history" in text.lower() for text in sent_messages)
            assert any("rolled back successfully" in text.lower() for text in sent_messages)

    asyncio.run(run())


def test_encrypted_streaming_range_response(tmp_path: Path) -> None:
    from fastapi import FastAPI

    from app.audio_extension.router import router

    async def run() -> None:
        source = make_wav() + bytes(range(256)) * 1_000
        cfg = settings(tmp_path)
        async with httpx.AsyncClient() as storage_client:
            store = AudioStore(cfg, storage_client)
            metadata = await store.replace(
                file_name="range.wav",
                mime_type="audio/wav",
                data=source,
                uploaded_by=200,
                telegram_file_id=None,
                telegram_update_id=1301,
            )

            app = FastAPI()
            app.state.audio_store = AudioStore(cfg, storage_client)
            app.include_router(router, prefix="/api")
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                response = await client.get(
                    "/api/audio/file",
                    params={"version": metadata.version},
                    headers={"Range": "bytes=1000-79999"},
                )
                assert response.status_code == 206
                assert response.content == source[1000:80000]
                assert response.headers["content-range"] == (
                    f"bytes 1000-79999/{len(source)}"
                )
                assert response.headers["accept-ranges"] == "bytes"
                assert int(response.headers["content-length"]) == 79_000

                suffix = await client.get(
                    "/api/audio/file",
                    params={"version": metadata.version},
                    headers={"Range": "bytes=-128"},
                )
                assert suffix.status_code == 206
                assert suffix.content == source[-128:]

                invalid = await client.get(
                    "/api/audio/file",
                    params={"version": metadata.version},
                    headers={"Range": f"bytes={len(source)}-"},
                )
                assert invalid.status_code == 416
                assert invalid.headers["content-range"] == f"bytes */{len(source)}"

    asyncio.run(run())


def test_decrypted_output_limit_is_enforced(tmp_path: Path) -> None:
    from app.audio_extension.store import AudioStoreError

    async def run() -> None:
        source = make_wav() + b"L" * 100_000
        cfg = settings(tmp_path)
        async with httpx.AsyncClient() as client:
            writer = AudioStore(cfg, client)
            metadata = await writer.replace(
                file_name="limit.wav",
                mime_type="audio/wav",
                data=source,
                uploaded_by=200,
                telegram_file_id=None,
                telegram_update_id=1401,
            )
            smaller_cfg = replace(cfg, max_bytes=len(source) - 1)
            reader = AudioStore(smaller_cfg, client)
            try:
                await reader.get_audio(requested_version=metadata.version)
            except AudioStoreError as exc:
                assert exc.code in {"stored_audio_too_large", "decrypted_audio_too_large"}
            else:
                raise AssertionError("Decrypted output larger than max_bytes must fail.")

    asyncio.run(run())


def test_failed_remote_upload_triggers_orphan_cleanup(tmp_path: Path) -> None:
    from app.audio_extension.store import AudioStoreError

    async def run() -> None:
        deleted_paths: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if url.endswith("/storage/v1/bucket/website-audio"):
                return httpx.Response(200, json={"id": "website-audio"})
            prefix = "https://project.supabase.co/storage/v1/object/website-audio/"
            if url.startswith(prefix):
                path = url[len(prefix):]
                if request.method == "POST" and path.startswith("versions/"):
                    # Simulate a remote failure after the server accepted bytes.
                    return httpx.Response(500, json={"message": "upload failed"})
                if request.method == "DELETE":
                    deleted_paths.append(path)
                    return httpx.Response(200, json={"message": "deleted"})
                if request.method == "GET":
                    return httpx.Response(
                        400,
                        json={
                            "statusCode": "404",
                            "error": "not_found",
                            "message": "Object not found",
                        },
                    )
            return httpx.Response(404)

        cfg = replace(
            settings(tmp_path),
            storage_mode="supabase",
            supabase_url="https://project.supabase.co",
            supabase_secret_key="service-key",
        )
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            store = AudioStore(cfg, client)
            try:
                await store.replace(
                    file_name="cleanup.wav",
                    mime_type="audio/wav",
                    data=make_wav(),
                    uploaded_by=200,
                    telegram_file_id=None,
                    telegram_update_id=1501,
                )
            except AudioStoreError as exc:
                assert exc.code == "supabase_storage_request_failed"
            else:
                raise AssertionError("Remote upload failure should be reported.")
            assert len(deleted_paths) == 1
            assert deleted_paths[0].startswith("versions/")

    asyncio.run(run())


def test_environment_loads_multiple_encryption_key_versions(monkeypatch) -> None:
    import base64

    monkeypatch.setenv("AUDIO_ENCRYPTION_KEY_V1", base64.b64encode(b"1" * 32).decode())
    monkeypatch.setenv("AUDIO_ENCRYPTION_KEY_V2", base64.b64encode(b"2" * 32).decode())
    cfg = AudioSettings.from_env()
    assert cfg.encryption_keys["v1"] == b"1" * 32
    assert cfg.encryption_keys["v2"] == b"2" * 32
    assert cfg.active_encryption_key == b"1" * 32


def test_history_limit_deletes_pruned_encrypted_objects(tmp_path: Path) -> None:
    async def run() -> None:
        cfg = replace(settings(tmp_path), history_limit=2)
        async with httpx.AsyncClient() as client:
            store = AudioStore(cfg, client)
            first = await store.replace(
                file_name="one.wav",
                mime_type="audio/wav",
                data=make_wav() + b"1" * 10_000,
                uploaded_by=200,
                telegram_file_id=None,
                telegram_update_id=1601,
            )
            second = await store.replace(
                file_name="two.wav",
                mime_type="audio/wav",
                data=make_wav() + b"2" * 10_000,
                uploaded_by=200,
                telegram_file_id=None,
                telegram_update_id=1602,
            )
            third = await store.replace(
                file_name="three.wav",
                mime_type="audio/wav",
                data=make_wav() + b"3" * 10_000,
                uploaded_by=200,
                telegram_file_id=None,
                telegram_update_id=1603,
            )
            history = await store.get_history(force=True)
            assert [item.version for item in history.items] == [
                third.version,
                second.version,
            ]
            assert not (tmp_path / first.object_path).exists()
            assert (tmp_path / second.object_path).is_file()
            assert (tmp_path / third.object_path).is_file()

    asyncio.run(run())


def test_if_range_mismatch_returns_full_encrypted_audio(tmp_path: Path) -> None:
    from fastapi import FastAPI

    from app.audio_extension.router import router

    async def run() -> None:
        source = make_wav() + b"R" * 100_000
        cfg = settings(tmp_path)
        async with httpx.AsyncClient() as storage_client:
            store = AudioStore(cfg, storage_client)
            metadata = await store.replace(
                file_name="if-range.wav",
                mime_type="audio/wav",
                data=source,
                uploaded_by=200,
                telegram_file_id=None,
                telegram_update_id=1701,
            )
            app = FastAPI()
            app.state.audio_store = AudioStore(cfg, storage_client)
            app.include_router(router, prefix="/api")
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                response = await client.get(
                    "/api/audio/file",
                    params={"version": metadata.version},
                    headers={
                        "Range": "bytes=10-99",
                        "If-Range": '"different-version"',
                    },
                )
                assert response.status_code == 200
                assert response.content == source
                assert "content-range" not in response.headers

    asyncio.run(run())
