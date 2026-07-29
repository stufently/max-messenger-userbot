"""События, состояние демона и его остановка."""

from __future__ import annotations

import asyncio
import contextlib

from fastapi import APIRouter, Depends, Query, Request, WebSocket, WebSocketDisconnect

from maxub.api.routes.common import MAX_PAGE_SIZE, get_service
from maxub.api.security import authenticate_websocket, require
from maxub.core.permissions import Scope
from maxub.core.service import UserbotService

router = APIRouter()

# Журнал и живой поток отдают payload событий, а в нём лежат тексты входящих
# сообщений. Поэтому мало права следить за состоянием: нужно и право читать
# переписку, иначе `events:read` тихо оказался бы способом её прочитать в обход
# `messages:read`.
JOURNAL = (Scope.EVENTS_READ, Scope.MESSAGES_READ)


@router.get("/health")
async def health() -> dict[str, str]:
    """Единственный маршрут без токена — нужен для healthcheck контейнера."""
    return {"status": "ok"}


@router.get("/status", dependencies=[Depends(require(Scope.ACCOUNTS_READ))])
async def status(service: UserbotService = Depends(get_service)) -> dict[str, object]:
    return await service.status()


@router.get("/events", dependencies=[Depends(require(*JOURNAL))])
async def events(
    limit: int = Query(default=50, ge=1, le=MAX_PAGE_SIZE),
    after_id: int = Query(default=0, ge=0),
    service: UserbotService = Depends(get_service),
) -> list[dict[str, object]]:
    """Журнал событий. Курсор ``after_id`` позволяет читать без повторов."""
    return await service.recent_events(limit=limit, after_id=after_id)


@router.post("/shutdown", dependencies=[Depends(require(Scope.ADMIN))])
async def shutdown(request: Request) -> dict[str, str]:
    request.app.state.shutdown_event.set()
    return {"status": "stopping"}


@router.websocket("/ws/events")
async def ws_events(websocket: WebSocket) -> None:
    """Поток событий. WebSocket используется только здесь и только для чтения."""
    if await authenticate_websocket(websocket, *JOURNAL) is None:
        return
    await websocket.accept()
    service: UserbotService = websocket.app.state.service
    queue = service.subscribe()
    try:
        while True:
            event = await queue.get()
            await websocket.send_json(event.model_dump(mode="json"))
    except (WebSocketDisconnect, asyncio.CancelledError):
        pass
    finally:
        service.unsubscribe(queue)
        with contextlib.suppress(Exception):
            await websocket.close()
