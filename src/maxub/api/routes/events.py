"""События, состояние демона и его остановка."""

from __future__ import annotations

import asyncio
import contextlib
import secrets

from fastapi import APIRouter, Depends, Query, Request, WebSocket, WebSocketDisconnect

from maxub.api.routes.common import MAX_PAGE_SIZE, get_service
from maxub.api.security import require_token
from maxub.core.service import UserbotService

router = APIRouter()


@router.get("/health")
async def health() -> dict[str, str]:
    """Единственный маршрут без токена — нужен для healthcheck контейнера."""
    return {"status": "ok"}


@router.get("/status", dependencies=[Depends(require_token)])
async def status(service: UserbotService = Depends(get_service)) -> dict[str, object]:
    return await service.status()


@router.get("/events", dependencies=[Depends(require_token)])
async def events(
    limit: int = Query(default=50, ge=1, le=MAX_PAGE_SIZE),
    after_id: int = Query(default=0, ge=0),
    service: UserbotService = Depends(get_service),
) -> list[dict[str, object]]:
    """Журнал событий. Курсор ``after_id`` позволяет читать без повторов."""
    return await service.recent_events(limit=limit, after_id=after_id)


@router.post("/shutdown", dependencies=[Depends(require_token)])
async def shutdown(request: Request) -> dict[str, str]:
    request.app.state.shutdown_event.set()
    return {"status": "stopping"}


@router.websocket("/ws/events")
async def ws_events(websocket: WebSocket) -> None:
    """Поток событий. WebSocket используется только здесь и только для чтения.

    Токен принимается заголовком, а не query-параметром: строка запроса
    попадает в логи прокси и диагностику, заголовок — нет. Сравнение
    постоянное по времени.
    """
    expected: str = websocket.app.state.api_token
    header = websocket.headers.get("authorization") or ""
    scheme, _, value = header.partition(" ")
    provided = value if scheme.lower() == "bearer" else header
    # Байты, а не строки: на не-ASCII символах сравнение строк бросает
    # TypeError, и соединение падало бы с ошибкой вместо закрытия кодом 4401.
    if not secrets.compare_digest(provided.strip().encode("utf-8"), expected.encode("utf-8")):
        await websocket.close(code=4401)
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
