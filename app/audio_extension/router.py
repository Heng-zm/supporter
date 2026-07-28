from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query, Request, Response, status

from .store import (
    AudioNotConfiguredError,
    AudioStore,
    AudioStoreError,
    AudioVersionChangedError,
    ObjectNotFound,
)

router = APIRouter(prefix="/audio", tags=["audio"])


def _store(request: Request) -> AudioStore:
    store = getattr(request.app.state, "audio_store", None)
    if not isinstance(store, AudioStore):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Audio extension is not initialized.",
            headers={"Retry-After": "5"},
        )
    return store


def _public_headers(response: Response) -> None:
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"


def _etag(version: str) -> str:
    return f'"{version}"'


def _etag_matches(raw_header: str, version: str) -> bool:
    expected = _etag(version)
    for item in raw_header.split(","):
        candidate = item.strip()
        if candidate == "*":
            return True
        if candidate.startswith("W/"):
            candidate = candidate[2:].strip()
        if candidate == expected:
            return True
    return False


def _storage_unavailable(exc: AudioStoreError) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={
            "message": "The audio storage service is temporarily unavailable.",
            "code": exc.code,
        },
        headers={"Retry-After": "5"},
    )


@router.get("/metadata", response_model=None)
async def audio_metadata(
    request: Request,
    response: Response,
) -> dict[str, object] | Response:
    _public_headers(response)
    response.headers["Cache-Control"] = "public, max-age=0, must-revalidate"

    try:
        metadata = await _store(request).get_metadata()
    except AudioNotConfiguredError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
            headers={"Retry-After": "5"},
        ) from exc
    except AudioStoreError as exc:
        raise _storage_unavailable(exc) from exc

    version = metadata.version if metadata is not None else "none"
    etag = _etag(version)
    common_headers = {
        "Cache-Control": "public, max-age=0, must-revalidate",
        "ETag": etag,
        "X-Content-Type-Options": "nosniff",
        "X-Robots-Tag": "noindex, nofollow",
    }
    if metadata is not None:
        common_headers["X-Audio-Version"] = metadata.version

    if _etag_matches(request.headers.get("if-none-match", ""), version):
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=common_headers)

    for key, value in common_headers.items():
        response.headers[key] = value

    if metadata is None:
        return {"ok": True, "available": False}
    return {"ok": True, **metadata.to_public_dict()}


@router.get("/file")
async def audio_file(
    request: Request,
    version: str | None = Query(default=None, min_length=1, max_length=160),
) -> Response:
    try:
        metadata, data = await _store(request).get_audio(
            requested_version=version
        )
    except ObjectNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No active audio is configured.",
        ) from exc
    except AudioVersionChangedError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": "The active audio changed. Refresh metadata and retry.",
                "currentVersion": str(exc),
            },
        ) from exc
    except AudioNotConfiguredError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
            headers={"Retry-After": "5"},
        ) from exc
    except AudioStoreError as exc:
        raise _storage_unavailable(exc) from exc

    ascii_name = metadata.file_name.encode("ascii", "ignore").decode("ascii") or "audio"
    encoded_name = quote(metadata.file_name, safe="")
    headers = {
        "Content-Length": str(len(data)),
        "Content-Disposition": (
            f'inline; filename="{ascii_name}"; filename*=UTF-8\'\'{encoded_name}'
        ),
        "ETag": _etag(metadata.version),
        "X-Audio-Version": metadata.version,
        "X-Content-Type-Options": "nosniff",
        "X-Robots-Tag": "noindex, nofollow",
        "Accept-Ranges": "none",
        "Cache-Control": (
            "public, max-age=31536000, immutable"
            if version
            else "no-store, max-age=0"
        ),
    }
    return Response(content=data, media_type=metadata.mime_type, headers=headers)
