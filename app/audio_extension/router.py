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
        )
    return store


def _public_headers(response: Response) -> None:
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"


@router.get("/metadata")
async def audio_metadata(request: Request, response: Response) -> dict[str, object]:
    _public_headers(response)
    response.headers["Cache-Control"] = "no-store, max-age=0"

    try:
        metadata = await _store(request).get_metadata()
    except AudioNotConfiguredError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except AudioStoreError as exc:
        raise HTTPException(
            status_code=502,
            detail="The audio storage service is temporarily unavailable.",
        ) from exc

    if metadata is None:
        return {"ok": True, "available": False}

    response.headers["ETag"] = f'"{metadata.version}"'
    response.headers["X-Audio-Version"] = metadata.version
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
        raise HTTPException(status_code=404, detail="No active audio is configured.") from exc
    except AudioVersionChangedError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "The active audio changed. Refresh metadata and retry.",
                "currentVersion": str(exc),
            },
        ) from exc
    except AudioNotConfiguredError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except AudioStoreError as exc:
        raise HTTPException(
            status_code=502,
            detail="The audio storage service is temporarily unavailable.",
        ) from exc

    ascii_name = metadata.file_name.encode("ascii", "ignore").decode("ascii") or "audio"
    encoded_name = quote(metadata.file_name, safe="")
    headers = {
        "Content-Length": str(len(data)),
        "Content-Disposition": (
            f'inline; filename="{ascii_name}"; filename*=UTF-8\'\'{encoded_name}'
        ),
        "ETag": f'"{metadata.version}"',
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
