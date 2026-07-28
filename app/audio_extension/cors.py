from __future__ import annotations

from urllib.parse import urlsplit

from .source_settings import BACKEND_CORS_ORIGINS


def get_backend_cors_origins() -> tuple[str, ...]:
    """Return validated browser origins for FastAPI CORSMiddleware.

    CORS origins must contain only scheme, host, and optional port. Paths,
    query strings, fragments, credentials, and trailing slashes are rejected
    or normalized so middleware matching remains exact and predictable.
    """

    normalized: list[str] = []
    seen: set[str] = set()

    for raw_value in BACKEND_CORS_ORIGINS:
        value = str(raw_value).strip().rstrip("/")
        parsed = urlsplit(value)

        if parsed.scheme not in {"http", "https"}:
            raise ValueError(
                "BACKEND_CORS_ORIGINS values must use http or https."
            )
        if not parsed.hostname:
            raise ValueError(
                "BACKEND_CORS_ORIGINS values must include a hostname."
            )
        if parsed.username or parsed.password:
            raise ValueError(
                "BACKEND_CORS_ORIGINS values must not contain credentials."
            )
        if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise ValueError(
                "BACKEND_CORS_ORIGINS values must be origins without paths, queries, or fragments."
            )

        host = parsed.hostname.lower()
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        port = f":{parsed.port}" if parsed.port is not None else ""
        origin = f"{parsed.scheme.lower()}://{host}{port}"

        if origin not in seen:
            seen.add(origin)
            normalized.append(origin)

    if not normalized:
        raise ValueError("BACKEND_CORS_ORIGINS cannot be empty.")

    return tuple(normalized)
