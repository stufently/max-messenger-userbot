"""Отправка сообщений и выгрузка истории."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from maxub.api.routes.common import (
    MAX_PAGE_SIZE,
    STUCK_PAGE_SIZE,
    SendRequest,
    StuckState,
    get_service,
    http_error,
)
from maxub.api.security import require_token
from maxub.core.models import OutboxState
from maxub.core.service import ServiceError, ServiceNotFound, UserbotService
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


@router.get("/outbox")
async def outbox(
    state: StuckState | None = None,
    limit: int = Query(default=STUCK_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    service: UserbotService = Depends(get_service),
) -> list[dict[str, object]]:
    """Записи очереди, которые ждут решения человека.

    Без ``state`` показываются отказавшие (`failed`) и застрявшие «в полёте»
    (`sending`): только они сами с места не сдвинутся. Ответ содержит ошибку,
    число попыток и время — по ним и принимается решение о повторе.
    """
    chosen = OutboxState(state.value) if state is not None else None
    return await service.list_stuck_messages(limit=limit, state=chosen)


@router.post("/outbox/{item_id}/retry")
async def retry(item_id: int, service: UserbotService = Depends(get_service)) -> dict[str, object]:
    """Повторяет отправку отказавшей записи по решению человека.

    ВНИМАНИЕ: сообщение могло дойти до получателя, и тогда повтор создаст у него
    дубль. Перед повтором демон сверяется с сервером, если транспорт это умеет:
    доказанная доставка закрывает запись без второй отправки. Если сверить не
    удалось, ответ содержит ``duplicate_risk: true`` — риск принимает вызывающий.

    Повтор разрешён только из состояния `failed`. Запись, которой распоряжается
    воркер (`queued`, `claimed`, `sending`), отбирать нельзя: это верный дубль,
    поэтому такой запрос отвергается конфликтом.
    """
    try:
        return await service.retry_message(item_id)
    except ServiceNotFound as exc:
        raise http_error(404, exc) from exc
    except ServiceError as exc:
        raise http_error(409, exc) from exc


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
