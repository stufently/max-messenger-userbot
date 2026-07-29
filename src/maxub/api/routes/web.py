"""Веб-интерфейс: страница управления аккаунтами и её операции.

Страницу отдаёт тот же демон — отдельного сервера нет (этап 1 в docs/stack.md).
Вход браузера и защита от CSRF вынесены в
[web_session][maxub.api.routes.web_session]; там же объяснено, почему у веба
собственная аутентификация вместо bearer-токена основного API. Одноразовый код
входа, которым пользуется автономная сборка под Windows, — в
[web_handoff][maxub.api.routes.web_handoff].

Операции продублированы под ``/web/api/*``, а не подключены к существующим
``/accounts`` и ``/login/*``: у тех маршрутов аутентификация зашита в сам
роутер, и подмешать к ним второй способ входа значило бы менять правила для
CLI. Обработчики здесь остаются краем — вся логика в `UserbotService`.
"""

from __future__ import annotations

import base64
from pathlib import Path

import qrcode
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse

from maxub.api.routes import web_handoff, web_session
from maxub.api.routes.common import (
    AccountRequest,
    AddAccountRequest,
    ChallengeRequest,
    DisableRequest,
    LoginCompleteRequest,
    get_service,
    http_error,
)
from maxub.api.routes.web_session import (
    SECURITY_HEADERS,
    require_local_host,
    require_web_session,
    secure_headers,
)
from maxub.core.service import ServiceError, ServiceOverloaded, UserbotService
from maxub.transport.base import TransportUnsupported

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
# Отдаём только известные файлы: обход каталога тогда невозможен в принципе.
STATIC_FILES = {
    "app.css": "text/css",
    "api.js": "text/javascript",
    "accounts.js": "text/javascript",
    "login.js": "text/javascript",
    "session.js": "text/javascript",
}

# Проверка Host и заголовки безопасности — на всей панели целиком, включая
# страницу и её файлы: пропуск любого из маршрутов открыл бы обход.
router = APIRouter(
    prefix="/web",
    include_in_schema=False,
    dependencies=[Depends(require_local_host), Depends(secure_headers)],
)
router.include_router(web_session.router)
# Одноразовый код входа: выдача закрыта bearer-токеном, обмен — открытый GET,
# но без действующего кода он лишь возвращает на страницу входа.
router.include_router(web_handoff.router)


# --- страница и её файлы -----------------------------------------------------


# Готовому Response заголовки из зависимости не достаются, поэтому файлам они
# проставляются здесь — иначе страница осталась бы без CSP.
@router.get("")
async def page() -> FileResponse:
    return FileResponse(
        STATIC_DIR / "index.html",
        media_type="text/html; charset=utf-8",
        headers=SECURITY_HEADERS,
    )


@router.get("/static/{name}")
async def asset(name: str) -> FileResponse:
    media_type = STATIC_FILES.get(name)
    if media_type is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="файл не найден")
    return FileResponse(
        STATIC_DIR / name,
        media_type=f"{media_type}; charset=utf-8",
        headers=SECURITY_HEADERS,
    )


# --- операции страницы -------------------------------------------------------

api = APIRouter(prefix="/api", dependencies=[Depends(require_web_session)])


@api.get("/state")
async def state(service: UserbotService = Depends(get_service)) -> dict[str, object]:
    """Состояние одним запросом: страница опрашивает его периодически.

    Поток `/ws/events` не подходит: браузер не умеет слать заголовок
    ``Authorization`` при открытии WebSocket, а токен в строке запроса оседает
    в логах. Опрос проще и не требует второго способа аутентификации.
    """
    return {
        "status": await service.status(),
        "accounts": [a.model_dump(mode="json") for a in await service.list_accounts()],
    }


@api.post("/accounts", status_code=201)
async def add_account(
    payload: AddAccountRequest, service: UserbotService = Depends(get_service)
) -> dict[str, object]:
    try:
        account = await service.add_account(payload.phone, payload.label)
    except ServiceError as exc:
        raise http_error(409, exc) from exc
    return account.model_dump(mode="json")


@api.post("/accounts/{account_id}/disable")
async def disable_account(
    account_id: int, payload: DisableRequest, service: UserbotService = Depends(get_service)
) -> dict[str, object]:
    try:
        account = await service.disable_account(account_id, payload.reason)
    except ServiceError as exc:
        raise http_error(404, exc) from exc
    return account.model_dump(mode="json")


@api.post("/login/start")
async def login_start(
    payload: AccountRequest, service: UserbotService = Depends(get_service)
) -> dict[str, str]:
    try:
        return {"challenge_id": await service.start_login(payload.account_id)}
    except ServiceOverloaded as exc:
        raise http_error(429, exc) from exc
    except ServiceError as exc:
        raise http_error(404, exc) from exc


@api.post("/login/complete")
async def login_complete(
    payload: LoginCompleteRequest, service: UserbotService = Depends(get_service)
) -> dict[str, object]:
    try:
        account = await service.complete_login(payload.challenge_id, payload.code)
    except ServiceError as exc:
        raise http_error(400, exc) from exc
    return account.model_dump(mode="json")


@api.post("/login/qr/start")
async def login_qr_start(
    payload: AccountRequest, service: UserbotService = Depends(get_service)
) -> dict[str, object]:
    try:
        challenge = await service.start_qr_login(payload.account_id)
    except TransportUnsupported as exc:
        raise http_error(501, exc) from exc
    except ServiceOverloaded as exc:
        raise http_error(429, exc) from exc
    except ServiceError as exc:
        raise http_error(404, exc) from exc
    return {**challenge, "image": qr_data_uri(str(challenge["payload"]))}


@api.post("/login/qr/poll")
async def login_qr_poll(
    payload: ChallengeRequest, service: UserbotService = Depends(get_service)
) -> dict[str, object]:
    try:
        status_value, account = await service.poll_qr_login(payload.challenge_id)
    except ServiceError as exc:
        raise http_error(404, exc) from exc
    return {
        "status": status_value.value,
        "account": account.model_dump(mode="json") if account else None,
    }


router.include_router(api)


def qr_data_uri(payload: str) -> str:
    """Рисует QR-код на сервере и возвращает его как data-URI.

    Отрисовка на сервере выбрана из-за запрета на сторонние скрипты: страница
    обязана работать без интернета, а тянуть в репозиторий чужую библиотеку QR
    ради одной картинки дороже, чем собрать SVG из матрицы — `qrcode` уже в
    зависимостях ради CLI. Data-URI избавляет страницу от вставки разметки
    через `innerHTML`.
    """
    code = qrcode.QRCode(border=2)
    code.add_data(payload)
    code.make(fit=True)
    matrix: list[list[bool]] = code.get_matrix()
    size = len(matrix)
    cells = "".join(
        f'<rect x="{x}" y="{y}" width="1" height="1"/>'
        for y, row in enumerate(matrix)
        for x, filled in enumerate(row)
        if filled
    )
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}" '
        f'shape-rendering="crispEdges" role="img" aria-label="QR-код для входа">'
        f'<rect width="{size}" height="{size}" fill="#ffffff"/>'
        f'<g fill="#000000">{cells}</g></svg>'
    )
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode("utf-8")).decode("ascii")
