"""HTTP- и WebSocket-маршруты. Тонкий входной адаптер: логика живёт в ядре."""

from __future__ import annotations

import asyncio
import contextlib
import secrets

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from pydantic import BaseModel, Field

from maxub.api.security import require_token
from maxub.core.service import ServiceError, UserbotService
from maxub.transport.base import TransportUnsupported

# Верхние границы отсекают запросы, способные раздуть БД или память демона.
MAX_TEXT_LENGTH = 4000
MAX_LABEL_LENGTH = 100
MAX_PAGE_SIZE = 500

router = APIRouter()


class AddAccountRequest(BaseModel):
    phone: str = Field(min_length=3, max_length=32)
    label: str | None = Field(default=None, max_length=MAX_LABEL_LENGTH)


class LoginStartRequest(BaseModel):
    account_id: int


class LoginCompleteRequest(BaseModel):
    challenge_id: str
    code: str


class SendRequest(BaseModel):
    account_id: int
    chat_id: str = Field(min_length=1, max_length=128)
    text: str = Field(min_length=1, max_length=MAX_TEXT_LENGTH)
    nonce: str | None = Field(default=None, max_length=128)


class DisableRequest(BaseModel):
    reason: str = Field(default="остановлен вручную", max_length=MAX_LABEL_LENGTH)


def get_service(request: Request) -> UserbotService:
    service: UserbotService = request.app.state.service
    return service


@router.get("/health")
async def health() -> dict[str, str]:
    """Единственный маршрут без токена — нужен для healthcheck контейнера."""
    return {"status": "ok"}


@router.get("/status", dependencies=[Depends(require_token)])
async def status(service: UserbotService = Depends(get_service)) -> dict[str, object]:
    return await service.status()


@router.get("/accounts", dependencies=[Depends(require_token)])
async def list_accounts(service: UserbotService = Depends(get_service)) -> list[dict[str, object]]:
    return [a.model_dump(mode="json") for a in await service.list_accounts()]


@router.post("/accounts", dependencies=[Depends(require_token)], status_code=201)
async def add_account(
    payload: AddAccountRequest, service: UserbotService = Depends(get_service)
) -> dict[str, object]:
    try:
        account = await service.add_account(payload.phone, payload.label)
    except ServiceError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return account.model_dump(mode="json")


@router.post("/accounts/{account_id}/disable", dependencies=[Depends(require_token)])
async def disable_account(
    account_id: int, payload: DisableRequest, service: UserbotService = Depends(get_service)
) -> dict[str, object]:
    try:
        account = await service.disable_account(account_id, payload.reason)
    except ServiceError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return account.model_dump(mode="json")


@router.post("/login/start", dependencies=[Depends(require_token)])
async def login_start(
    payload: LoginStartRequest, service: UserbotService = Depends(get_service)
) -> dict[str, str]:
    try:
        challenge_id = await service.start_login(payload.account_id)
    except ServiceError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"challenge_id": challenge_id}


@router.post("/login/complete", dependencies=[Depends(require_token)])
async def login_complete(
    payload: LoginCompleteRequest, service: UserbotService = Depends(get_service)
) -> dict[str, object]:
    try:
        account = await service.complete_login(payload.challenge_id, payload.code)
    except ServiceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return account.model_dump(mode="json")


@router.post("/send", dependencies=[Depends(require_token)], status_code=202)
async def send(
    payload: SendRequest, service: UserbotService = Depends(get_service)
) -> dict[str, object]:
    try:
        item, created = await service.enqueue_message(
            payload.account_id, payload.chat_id, payload.text, payload.nonce
        )
    except TransportUnsupported as exc:
        raise HTTPException(status_code=501, detail=str(exc)) from exc
    except ServiceError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"queued": created, "item": item.model_dump(mode="json")}


@router.get("/history", dependencies=[Depends(require_token)])
async def history(
    account_id: int,
    chat_id: str,
    limit: int = Query(default=20, ge=1, le=MAX_PAGE_SIZE),
    service: UserbotService = Depends(get_service),
) -> list[dict[str, object]]:
    try:
        return await service.fetch_history(account_id, chat_id, limit)
    except TransportUnsupported as exc:
        raise HTTPException(status_code=501, detail=str(exc)) from exc
    except ServiceError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/events", dependencies=[Depends(require_token)])
async def events(
    limit: int = Query(default=50, ge=1, le=MAX_PAGE_SIZE),
    after_id: int = Query(default=0, ge=0),
    service: UserbotService = Depends(get_service),
) -> list[dict[str, object]]:
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
    if not secrets.compare_digest(provided.strip(), expected):
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
