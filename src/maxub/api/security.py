"""Аутентификация локального API и проверка прав.

Токен обязателен даже на петлевом интерфейсе: внутри одного контейнера или хоста
соседний процесс иначе получил бы доступ бесплатно.

Токен отвечает на вопрос «кто», области доступа — на вопрос «что можно». Раньше
второго вопроса не было вовсе: знание токена означало полный контроль над всеми
аккаунтами. Разбор — в [permissions][maxub.core.permissions], проверка самих
токенов — в [access][maxub.core.access]; здесь только край HTTP.

Коды ответов различаются по смыслу: 401 — «не понял, кто вы» (токена нет, он
неверен, отозван или просрочен), 403 — «понял, но этого вам нельзя». Слить их в
один код значило бы заставить клиента гадать, менять токен или просить права.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import Depends, Header, HTTPException, Request, WebSocket, status

from maxub.core.access import AccessControl, Principal
from maxub.core.permissions import Scope

#: Коды закрытия WebSocket. Своя пара, потому что HTTP-статусы там недоступны:
#: 4401 — не опознан, 4403 — прав не хватает.
WS_UNAUTHORIZED = 4401
WS_FORBIDDEN = 4403


def get_access(request: Request) -> AccessControl:
    access: AccessControl = request.app.state.access
    return access


def bearer_value(header: str | None) -> str | None:
    """Достаёт токен из заголовка ``Authorization``.

    Схема необязательна: клиенты, присылающие голый токен, работали так с самого
    начала, и ломать их ради формальности незачем.
    """
    if not header:
        return None
    scheme, _, value = header.partition(" ")
    return value if scheme.lower() == "bearer" else header


async def authenticate(
    request: Request,
    authorization: str | None = Header(default=None),
) -> Principal:
    """Опознаёт предъявителя или отказывает с 401."""
    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="требуется заголовок Authorization"
        )
    principal = await get_access(request).authenticate(bearer_value(authorization))
    if principal is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="токен неверен, отозван или просрочен",
        )
    return principal


def require(*scopes: Scope) -> Callable[..., Awaitable[Principal]]:
    """Зависимость маршрута: нужен токен и перечисленные области доступа.

    Области перечисляются на самом маршруте, а не на роутере целиком: чтение и
    запись живут в одном файле рядом, и общая зависимость на роутер означала бы
    самое широкое право из набора для всех его маршрутов сразу.
    """

    async def dependency(principal: Principal = Depends(authenticate)) -> Principal:
        missing = principal.missing(scopes)
        if missing:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"токену не хватает прав: {', '.join(scope.value for scope in missing)}",
            )
        return principal

    return dependency


async def authenticate_websocket(websocket: WebSocket, *scopes: Scope) -> Principal | None:
    """Опознаёт предъявителя у WebSocket и сам закрывает соединение при отказе.

    Токен принимается заголовком, а не query-параметром: строка запроса попадает
    в логи прокси и диагностику, заголовок — нет.
    """
    access: AccessControl = websocket.app.state.access
    principal = await access.authenticate(bearer_value(websocket.headers.get("authorization")))
    if principal is None:
        await websocket.close(code=WS_UNAUTHORIZED)
        return None
    if principal.missing(scopes):
        await websocket.close(code=WS_FORBIDDEN)
        return None
    return principal
