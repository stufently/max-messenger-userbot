"""Отправка сообщений и выгрузка истории."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from maxub.api.routes.common import (
    MAX_PAGE_SIZE,
    STUCK_PAGE_SIZE,
    DiscardRequest,
    SendRequest,
    StuckState,
    get_service,
    http_error,
)
from maxub.api.security import require
from maxub.core.models import OutboxState
from maxub.core.permissions import Scope
from maxub.core.service import ServiceError, ServiceNotFound, UserbotService
from maxub.transport.base import TransportUnsupported

router = APIRouter()

READ = Depends(require(Scope.MESSAGES_READ))
# Право отправлять сообщения от лица владельца — самое дорогое в наборе:
# получатель видит их как написанные человеком. Разбор очереди (повтор и отказ)
# сюда же: повтор — это отправка, а отказ — окончательный отказ от неё.
WRITE = Depends(require(Scope.MESSAGES_WRITE))


@router.post("/send", status_code=202, dependencies=[WRITE])
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


@router.get("/outbox", dependencies=[READ])
async def outbox(
    state: StuckState | None = None,
    limit: int = Query(default=STUCK_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    service: UserbotService = Depends(get_service),
) -> list[dict[str, object]]:
    """Записи очереди, которые ждут решения человека.

    Без ``state`` показываются отказавшие (`failed`) и застрявшие «в полёте»
    (`sending`): только они сами с места не сдвинутся. Ответ содержит ошибку,
    число попыток и время — по ним и принимается решение о повторе.

    Фильтр `discarded` показывает уже разобранное — записи, от которых человек
    отказался, вместе с причиной отказа. В выдачу без фильтра они не попадают:
    решение по ним принято, и звать к ним человека второй раз незачем.
    """
    chosen = OutboxState(state.value) if state is not None else None
    return await service.list_stuck_messages(limit=limit, state=chosen)


@router.post("/outbox/{item_id}/retry", dependencies=[WRITE])
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


@router.post("/outbox/{item_id}/discard", dependencies=[WRITE])
async def discard(
    item_id: int, payload: DiscardRequest, service: UserbotService = Depends(get_service)
) -> dict[str, object]:
    """Отказывается от записи: сообщение не будет отправлено никогда.

    Второй возможный итог разбора рядом с повтором. Решение окончательно:
    вернуть запись в очередь после отказа нельзя, передумавший ставит новое
    сообщение. Причина обязательна и сохраняется отдельно от ошибки отправки —
    та понадобится, когда к записи вернутся позже.

    Отказаться, как и повторить, можно только из состояния `failed`: запись, с
    которой работает воркер, отменять на ходу нельзя — отправка уже могла уйти в
    сеть. Такой запрос отвергается конфликтом.
    """
    try:
        return await service.discard_message(item_id, payload.reason)
    except ServiceNotFound as exc:
        raise http_error(404, exc) from exc
    except ServiceError as exc:
        raise http_error(409, exc) from exc


@router.get("/history", dependencies=[READ])
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
