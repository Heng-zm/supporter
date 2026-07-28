from __future__ import annotations

from contextlib import asynccontextmanager
import hmac
import os

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware

from app.audio_extension import (
    close_audio_extension,
    get_backend_cors_origins,
    handle_audio_telegram_update,
    include_audio_router,
    start_audio_extension,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await start_audio_extension(app)
    try:
        yield
    finally:
        await close_audio_extension(app)


app = FastAPI(title="Audio Extension Demo", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(get_backend_cors_origins()),
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Accept", "Content-Type", "X-Telegram-Bot-Api-Secret-Token"],
    expose_headers=["ETag", "X-Audio-Version"],
)
include_audio_router(app, api_prefix="/api")


@app.get("/health")
async def health() -> dict[str, bool]:
    return {"ok": True}


@app.post("/api/telegram/webhook")
async def telegram_webhook(request: Request) -> dict[str, object]:
    expected = (
        os.getenv("TELEGRAM_WEBHOOK_SECRET", "").strip()
        or os.getenv("TELEGRAM_WEBHOOK_SECRET_TOKEN", "").strip()
    )
    received = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "").strip()
    if not expected:
        raise HTTPException(status_code=503, detail="Telegram webhook secret is not configured.")
    if not hmac.compare_digest(expected, received):
        raise HTTPException(status_code=403, detail="Invalid Telegram webhook secret.")

    update = await request.json()
    if not isinstance(update, dict):
        raise HTTPException(status_code=400, detail="Telegram update must be a JSON object.")

    handled = await handle_audio_telegram_update(request.app, update)
    return {"ok": True, "handled": handled}
