from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import ValidationError

from app.dependencies import get_visit_crypto, get_visit_service, require_json_content_type
from app.models import VisitPayload, VisitResponse
from app.services.visit_crypto import ENCRYPTION_NAME, VisitCryptoService
from app.services.visits import VisitRateLimitError, VisitService


logger = logging.getLogger("app.visits")
router = APIRouter(prefix="/website", tags=["visits"])


@router.get("/public-key")
async def website_visit_public_key(
    response: Response,
    crypto: VisitCryptoService = Depends(get_visit_crypto),
) -> dict[str, str | bool]:
    if not crypto.enabled:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Visit encryption is not configured.",
        )
    response.headers["Cache-Control"] = "public, max-age=3600"
    response.headers["Vary"] = "Origin"
    return {
        "ok": True,
        "algorithm": ENCRYPTION_NAME,
        "publicKey": crypto.public_key_b64(),
    }


@router.post(
    "/visit",
    response_model=VisitResponse,
    dependencies=[Depends(require_json_content_type)],
)
async def website_visit(
    request: Request,
    response: Response,
    body: dict[str, Any],
    visits: VisitService = Depends(get_visit_service),
    crypto: VisitCryptoService = Depends(get_visit_crypto),
) -> VisitResponse:
    response.headers["Cache-Control"] = "no-store, max-age=0"

    try:
        await visits.enforce_rate_limit(request)
        if body.get("encryption") == ENCRYPTION_NAME:
            payload = crypto.decrypt(body)
        elif crypto.settings.require_encrypted_visits:
            if not crypto.enabled:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Visit encryption is temporarily unavailable.",
                )
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Encrypted visit payload is required.",
            )
        else:
            payload = VisitPayload.model_validate(body)

        result = await visits.process(request, payload)
    except VisitRateLimitError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many visit requests.",
            headers={"Retry-After": str(exc.retry_after_seconds)},
        ) from exc
    except HTTPException:
        raise
    except (ValueError, ValidationError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid visit payload.",
        ) from exc
    except RuntimeError as exc:
        request_id = getattr(request.state, "request_id", "unknown")
        logger.warning(
            "Visit service unavailable: request_id=%s",
            request_id,
            exc_info=(type(exc), exc, exc.__traceback__),
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Visit service is temporarily unavailable.",
        ) from exc

    return VisitResponse(
        duplicate=result.duplicate,
        stored=result.stored,
        sent=result.telegram.ok,
        telegram_skipped=result.telegram.skipped,
    )
