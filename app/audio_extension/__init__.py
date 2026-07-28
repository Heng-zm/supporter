from .cors import get_backend_cors_origins
from .ptb_adapter import register_python_telegram_bot_handler
from .integration import (
    close_audio_extension,
    handle_audio_telegram_update,
    include_audio_router,
    start_audio_extension,
)

__all__ = [
    "get_backend_cors_origins",
    "close_audio_extension",
    "handle_audio_telegram_update",
    "include_audio_router",
    "start_audio_extension",
    "register_python_telegram_bot_handler",
]
