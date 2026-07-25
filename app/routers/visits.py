from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.dependencies import get_visit_service
from app.models import VisitPayload, VisitResponse
from app.services.visits import VisitService

router = APIRouter(prefix="/website", tags=["visits"])


@router.post("/visit", response_model=VisitResponse)
async def website_visit(
    request: Request,
    payload: VisitPayload,
    visits: VisitService = Depends(get_visit_service),
) -> VisitResponse:
    try:
        result = await visits.process(request, payload)
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc

    return VisitResponse(
        duplicate=result.duplicate,
        stored=result.stored,
        sent=result.telegram.ok,
        telegram_skipped=result.telegram.skipped,
    )
