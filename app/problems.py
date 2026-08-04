from __future__ import annotations

import logging
from http import HTTPStatus
from typing import Any

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import Scope

from app.utils.security import random_id

logger = logging.getLogger("app.errors")

PROBLEM_MEDIA_TYPE = "application/problem+json"


class ValidationProblemItem(BaseModel):
    pointer: str
    detail: str
    code: str


class ProblemDetails(BaseModel):
    model_config = ConfigDict(extra="allow")

    type: str
    title: str
    status: int
    detail: str
    instance: str
    errorCode: str
    requestId: str
    errors: list[ValidationProblemItem] | None = None


PROBLEM_RESPONSES = {
    status_code: {
        "description": f"{_description} (RFC 9457 Problem Details)",
        "content": {
            PROBLEM_MEDIA_TYPE: {"schema": ProblemDetails.model_json_schema()}
        },
    }
    for status_code, _description in {
        400: "Bad request",
        401: "Authentication required",
        403: "Forbidden",
        404: "Resource not found",
        409: "Conflict",
        413: "Request body too large",
        415: "Unsupported media type",
        422: "Request validation failed",
        429: "Rate limit exceeded",
        500: "Internal server error",
        502: "Upstream service unavailable",
        503: "Service unavailable",
    }.items()
}

_DEFAULT_ERROR_CODES = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    413: "payload_too_large",
    415: "unsupported_media_type",
    422: "validation_error",
    429: "rate_limit_exceeded",
    500: "internal_server_error",
    502: "upstream_unavailable",
    503: "service_unavailable",
}


def problem_detail(message: str, code: str, **extensions: Any) -> dict[str, Any]:
    return {"message": message, "code": code, **extensions}


def _request_id_from_scope(scope: Scope) -> str:
    state = scope.get("state")
    if isinstance(state, dict):
        value = str(state.get("request_id") or "").strip()
        if value:
            return value
    return random_id()


def _title(status_code: int) -> str:
    try:
        return HTTPStatus(status_code).phrase
    except ValueError:
        return "HTTP error"


def _detail_parts(
    status_code: int,
    detail: Any,
    error_code: str | None,
    extensions: dict[str, Any] | None,
) -> tuple[str, str, dict[str, Any]]:
    extra = dict(extensions or {})
    code = error_code or _DEFAULT_ERROR_CODES.get(status_code, "http_error")

    if isinstance(detail, dict):
        message = str(detail.get("message") or detail.get("detail") or _title(status_code))
        raw_code = detail.get("code")
        if raw_code:
            code = str(raw_code)
        for key, value in detail.items():
            if key not in {"message", "detail", "code"}:
                extra.setdefault(str(key), value)
    elif isinstance(detail, str) and detail.strip():
        message = detail.strip()
    else:
        message = _title(status_code)

    return message, code, extra


def problem_response(
    scope: Scope,
    *,
    status_code: int,
    detail: Any,
    error_code: str | None = None,
    title: str | None = None,
    headers: dict[str, str] | None = None,
    extensions: dict[str, Any] | None = None,
) -> JSONResponse:
    request_id = _request_id_from_scope(scope)
    message, code, extra = _detail_parts(
        status_code,
        detail,
        error_code,
        extensions,
    )
    payload: dict[str, Any] = {
        "type": f"urn:ozo-api:problem:{code}",
        "title": title or _title(status_code),
        "status": status_code,
        "detail": message,
        "instance": f"urn:ozo-api:request:{request_id}",
        "errorCode": code,
        "requestId": request_id,
        **extra,
    }
    response_headers = {
        key: value
        for key, value in (headers or {}).items()
        if key.lower() not in {"content-length", "content-type"}
    }
    response_headers.setdefault("Cache-Control", "no-store")
    return JSONResponse(
        status_code=status_code,
        content=payload,
        headers=response_headers,
        media_type=PROBLEM_MEDIA_TYPE,
    )


async def http_exception_handler(
    request: Request,
    exc: StarletteHTTPException,
) -> JSONResponse:
    return problem_response(
        request.scope,
        status_code=exc.status_code,
        detail=exc.detail,
        headers=dict(exc.headers or {}),
    )


def _validation_pointer(location: tuple[Any, ...]) -> str:
    parts = [str(part) for part in location if part not in {"body", "query"}]
    escaped = [part.replace("~", "~0").replace("/", "~1") for part in parts]
    return "/" + "/".join(escaped) if escaped else "/"


async def validation_exception_handler(
    request: Request,
    exc: RequestValidationError,
) -> JSONResponse:
    errors = [
        {
            "pointer": _validation_pointer(tuple(error.get("loc") or ())),
            "detail": str(error.get("msg") or "Invalid value."),
            "code": str(error.get("type") or "value_error"),
        }
        for error in exc.errors()
    ]
    return problem_response(
        request.scope,
        status_code=422,
        title="Request validation failed",
        detail="One or more request fields are invalid.",
        error_code="validation_error",
        extensions={"errors": errors},
    )


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    request_id = _request_id_from_scope(request.scope)
    logger.error(
        "Unhandled API exception: request_id=%s method=%s path=%s",
        request_id,
        request.method,
        request.url.path,
        exc_info=(type(exc), exc, exc.__traceback__),
    )
    return problem_response(
        request.scope,
        status_code=500,
        detail="The server could not complete the request.",
        error_code="internal_server_error",
    )
