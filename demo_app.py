from __future__ import annotations

from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.audio_extension import (
    close_audio_extension,
    get_backend_cors_origins,
    configure_audio_telegram_webhook,
    include_audio_router,
    include_audio_telegram_webhook_router,
    start_audio_extension,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await start_audio_extension(app)
    await configure_audio_telegram_webhook(app, api_prefix="/api")
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
include_audio_telegram_webhook_router(app, api_prefix="/api")


@app.get("/health")
async def health() -> dict[str, bool]:
    return {"ok": True}
