"""Аутентификация локального API.

API даёт полный контроль над аккаунтами, поэтому токен обязателен даже на
петлевом интерфейсе: внутри одного контейнера или хоста соседний процесс иначе
получил бы доступ бесплатно.
"""

from __future__ import annotations

import secrets

from fastapi import Header, HTTPException, Request, status


async def require_token(
    request: Request,
    authorization: str | None = Header(default=None),
) -> None:
    expected: str = request.app.state.api_token
    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="требуется заголовок Authorization"
        )
    scheme, _, value = authorization.partition(" ")
    provided = value if scheme.lower() == "bearer" else authorization
    if not secrets.compare_digest(provided.strip(), expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="неверный токен")
