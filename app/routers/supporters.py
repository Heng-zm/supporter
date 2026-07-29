from __future__ import annotations

import logging
from decimal import Decimal
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status

from app.config import Settings
from app.dependencies import (
    get_app_settings,
    get_supabase,
    require_admin,
    require_json_content_type,
)
from app.models import (
    DeleteResponse,
    SupporterCreate,
    SupporterResponse,
    SupportersResponse,
    SupporterUpdate,
)
from app.services.supabase import SupabaseError, SupabaseService

logger = logging.getLogger("app.supporters")
router = APIRouter(prefix="/supporters", tags=["supporters"])


def _json_row(model: SupporterCreate | SupporterUpdate) -> dict[str, Any]:
    data = model.model_dump(exclude_unset=True)
    for key, value in list(data.items()):
        if isinstance(value, Decimal):
            data[key] = float(value)
    return data


def _provider_error() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail="The supporter database is temporarily unavailable.",
    )


def _set_public_headers(response: Response, source: str, stale: bool) -> None:
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["X-Supporters-Source"] = source
    if stale:
        response.headers["Warning"] = '110 - "Response is stale"'


@router.get("", response_model=SupportersResponse)
async def list_supporters(
    response: Response,
    limit: Annotated[int | None, Query(ge=1, le=500)] = None,
    settings: Settings = Depends(get_app_settings),
    supabase: SupabaseService = Depends(get_supabase),
) -> SupportersResponse:
    requested_limit = min(limit or settings.max_supporters, settings.max_supporters)

    if not supabase.enabled:
        source = "not-configured"
        _set_public_headers(response, source, False)
        return SupportersResponse(supporters=[], source=source)

    try:
        rows, source, stale = await supabase.list_supporters_resilient(requested_limit)
    except SupabaseError as exc:
        raise _provider_error() from exc

    _set_public_headers(response, source, stale)
    return SupportersResponse(supporters=rows, source=source, stale=stale)


@router.post(
    "",
    response_model=SupporterResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_json_content_type)],
)
async def create_supporter(
    request: Request,
    response: Response,
    payload: SupporterCreate,
    _: None = Depends(require_admin),
    supabase: SupabaseService = Depends(get_supabase),
) -> SupporterResponse:
    response.headers["Cache-Control"] = "no-store, max-age=0"
    if not supabase.enabled:
        raise HTTPException(status_code=503, detail="Supabase is not configured.")
    try:
        row = await supabase.create_supporter(_json_row(payload))
        logger.info(
            "Supporter created through REST admin API: request_id=%s supporter_id=%s",
            getattr(request.state, "request_id", "unknown"),
            row.get("id", "unknown"),
        )
        return SupporterResponse(supporter=row)
    except SupabaseError as exc:
        raise _provider_error() from exc


@router.patch(
    "/{supporter_id}",
    response_model=SupporterResponse,
    dependencies=[Depends(require_json_content_type)],
)
async def update_supporter(
    supporter_id: UUID,
    request: Request,
    response: Response,
    payload: SupporterUpdate,
    _: None = Depends(require_admin),
    supabase: SupabaseService = Depends(get_supabase),
) -> SupporterResponse:
    response.headers["Cache-Control"] = "no-store, max-age=0"
    if not supabase.enabled:
        raise HTTPException(status_code=503, detail="Supabase is not configured.")
    try:
        row = await supabase.update_supporter(str(supporter_id), _json_row(payload))
    except SupabaseError as exc:
        raise _provider_error() from exc
    if row is None:
        raise HTTPException(status_code=404, detail="Supporter not found.")
    logger.info(
        "Supporter updated through REST admin API: request_id=%s supporter_id=%s",
        getattr(request.state, "request_id", "unknown"),
        supporter_id,
    )
    return SupporterResponse(supporter=row)


@router.delete("/{supporter_id}", response_model=DeleteResponse)
async def delete_supporter(
    supporter_id: UUID,
    request: Request,
    response: Response,
    _: None = Depends(require_admin),
    supabase: SupabaseService = Depends(get_supabase),
) -> DeleteResponse:
    response.headers["Cache-Control"] = "no-store, max-age=0"
    if not supabase.enabled:
        raise HTTPException(status_code=503, detail="Supabase is not configured.")
    try:
        deleted = await supabase.delete_supporter(str(supporter_id))
    except SupabaseError as exc:
        raise _provider_error() from exc
    if not deleted:
        raise HTTPException(status_code=404, detail="Supporter not found.")
    logger.info(
        "Supporter deleted through REST admin API: request_id=%s supporter_id=%s",
        getattr(request.state, "request_id", "unknown"),
        supporter_id,
    )
    return DeleteResponse()
