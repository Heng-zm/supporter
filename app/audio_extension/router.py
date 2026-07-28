from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from fastapi.responses import StreamingResponse

from .store import (
    AudioNotConfiguredError,
    AudioStore,
    AudioStoreError,
    AudioVersionChangedError,
    ObjectNotFound,
)

router = APIRouter(prefix="/audio", tags=["audio"])


@dataclass(frozen=True, slots=True)
class ByteRange:
    start: int
    end: int

    @property
    def length(self) -> int:
        return self.end - self.start + 1


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


def _parse_range(raw: str, total: int) -> ByteRange | None:
    value = raw.strip()
    if not value:
        return None
    if not value.lower().startswith("bytes=") or "," in value:
        raise ValueError("Only one bytes range is supported.")
    spec = value[6:].strip()
    if "-" not in spec:
        raise ValueError("Invalid Range header.")
    start_text, end_text = spec.split("-", 1)

    if not start_text:
        try:
            suffix_length = int(end_text)
        except ValueError as exc:
            raise ValueError("Invalid suffix range.") from exc
        if suffix_length <= 0:
            raise ValueError("Invalid suffix range.")
        suffix_length = min(suffix_length, total)
        return ByteRange(total - suffix_length, total - 1)

    try:
        start = int(start_text)
    except ValueError as exc:
        raise ValueError("Invalid range start.") from exc
    if start < 0 or start >= total:
        raise ValueError("Range start is outside the audio file.")

    if not end_text:
        return ByteRange(start, total - 1)
    try:
        end = int(end_text)
    except ValueError as exc:
        raise ValueError("Invalid range end.") from exc
    if end < start:
        raise ValueError("Range end is before range start.")
    return ByteRange(start, min(end, total - 1))


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


@router.get("/file", response_class=StreamingResponse)
async def audio_file(
    request: Request,
    version: str | None = Query(default=None, min_length=1, max_length=160),
) -> StreamingResponse:
    store = _store(request)
    try:
        metadata = await store.get_metadata(force=bool(version))
        if metadata is None:
            raise ObjectNotFound("No active audio is configured.")
        if version and version != metadata.version:
            raise AudioVersionChangedError(metadata.version)

        byte_range: ByteRange | None = None
        raw_range = request.headers.get("range", "")
        if_range = request.headers.get("if-range", "").strip()
        if raw_range and if_range and if_range != _etag(metadata.version):
            # RFC range semantics: when If-Range does not match the current
            # entity tag, ignore Range and return the complete representation.
            raw_range = ""
        if raw_range:
            if not store.settings.range_requests_enabled:
                raise HTTPException(
                    status_code=status.HTTP_416_RANGE_NOT_SATISFIABLE,
                    detail="Audio byte-range requests are disabled.",
                    headers={"Content-Range": f"bytes */{metadata.byte_length}"},
                )
            try:
                byte_range = _parse_range(raw_range, metadata.byte_length)
            except ValueError as exc:
                raise HTTPException(
                    status_code=status.HTTP_416_RANGE_NOT_SATISFIABLE,
                    detail=str(exc),
                    headers={"Content-Range": f"bytes */{metadata.byte_length}"},
                ) from exc

        metadata, stream = await store.stream_audio(
            requested_version=version,
            byte_range=(byte_range.start, byte_range.end) if byte_range else None,
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
    content_length = byte_range.length if byte_range else metadata.byte_length
    headers = {
        "Content-Length": str(content_length),
        "Content-Disposition": (
            f'inline; filename="{ascii_name}"; filename*=UTF-8\'\'{encoded_name}'
        ),
        "ETag": _etag(metadata.version),
        "X-Audio-Version": metadata.version,
        "X-Content-Type-Options": "nosniff",
        "X-Robots-Tag": "noindex, nofollow",
        "Accept-Ranges": "bytes" if store.settings.range_requests_enabled else "none",
        "Cache-Control": (
            "public, max-age=31536000, immutable"
            if version
            else "no-store, max-age=0"
        ),
    }
    response_status = status.HTTP_200_OK
    if byte_range is not None:
        response_status = status.HTTP_206_PARTIAL_CONTENT
        headers["Content-Range"] = (
            f"bytes {byte_range.start}-{byte_range.end}/{metadata.byte_length}"
        )

    return StreamingResponse(
        stream,
        status_code=response_status,
        media_type=metadata.mime_type,
        headers=headers,
    )
