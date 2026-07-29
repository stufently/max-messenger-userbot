"""Отправка сообщений и выгрузка истории."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from maxub.api.routes.common import MAX_PAGE_SIZE, SendRequest, get_service, http_error
from maxub.api.security import require_token
from maxub.core.service import ServiceError, UserbotService
from maxub.transport.base import TransportUnsupported

router = APIRouter(dependencies=[Depends(require_token)])


@router.post("/send", status_code=202)
async def send(
    payload: SendRequest, service: UserbotService = Depends(get_service)
) -> dict[str, object]:
    """Ставит сообщение в очередь.

    Ответ 202 означает «принято к отправке», а не «доставлено»: за доставку
    отвечает очередь, её исход виден в событиях.
    """
    try:
        item, created = await service.enqueue_message(
            payload.account_id, payload.chat_id, payload.text, payload.nonce
        )
    except TransportUnsupported as exc:
        raise http_error(501, exc) from exc
    except ServiceError as exc:
        raise http_error(409, exc) from exc
    return {"queued": created, "item": item.model_dump(mode="json")}


@router.get("/history")
async def history(
    account_id: int,
    chat_id: str,
    limit: int = Query(default=20, ge=1, le=MAX_PAGE_SIZE),
    service: UserbotService = Depends(get_service),
) -> list[dict[str, object]]:
    try:
        return await service.fetch_history(account_id, chat_id, limit)
    except TransportUnsupported as exc:
        raise http_error(501, exc) from exc
    except ServiceError as exc:
        raise http_error(404, exc) from exc
