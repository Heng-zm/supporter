from __future__ import annotations

import os

import uvicorn


def _bounded_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw_value = os.getenv(name, str(default)).strip()
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer.") from exc
    if not minimum <= value <= maximum:
        raise RuntimeError(f"{name} must be between {minimum} and {maximum}.")
    return value


def main() -> None:
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=_bounded_int("PORT", 8000, 1, 65535),
        workers=_bounded_int("WEB_CONCURRENCY", 1, 1, 8),
        proxy_headers=False,
        forwarded_allow_ips="",
        server_header=False,
        access_log=False,
        log_level=os.getenv("LOG_LEVEL", "info").strip().lower() or "info",
        limit_concurrency=_bounded_int("UVICORN_LIMIT_CONCURRENCY", 100, 10, 10000),
        backlog=_bounded_int("UVICORN_BACKLOG", 128, 16, 4096),
        limit_max_requests=_bounded_int("UVICORN_MAX_REQUESTS", 10000, 100, 1000000),
        limit_max_requests_jitter=_bounded_int(
            "UVICORN_MAX_REQUESTS_JITTER",
            1000,
            0,
            100000,
        ),
        timeout_keep_alive=_bounded_int("UVICORN_KEEP_ALIVE_SECONDS", 5, 1, 30),
        timeout_graceful_shutdown=_bounded_int(
            "UVICORN_GRACEFUL_SHUTDOWN_SECONDS",
            20,
            1,
            120,
        ),
        timeout_worker_healthcheck=_bounded_int(
            "UVICORN_WORKER_HEALTHCHECK_SECONDS",
            5,
            1,
            30,
        ),
        reset_contextvars=True,
    )


if __name__ == "__main__":
    main()
