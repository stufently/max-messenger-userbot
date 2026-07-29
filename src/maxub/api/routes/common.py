"""Общее для маршрутов: схемы запросов, границы, доступ к сервису."""

from __future__ import annotations

from enum import StrEnum

from fastapi import HTTPException, Request
from pydantic import BaseModel, Field

from maxub.core.models import OutboxState
from maxub.core.service import UserbotService

# Верхние границы отсекают запросы, способные раздуть БД или память демона.
MAX_TEXT_LENGTH = 4000
MAX_LABEL_LENGTH = 100
MAX_PAGE_SIZE = 500

# Разбор застрявших записей — работа глазами: страница по умолчанию короче
# предельной, чтобы человек видел список целиком, а не его хвост.
STUCK_PAGE_SIZE = 50


class StuckState(StrEnum):
    """Состояния, которые вообще имеет смысл разбирать вручную.

    Отдельный тип, а не весь ``OutboxState``: запрос очереди на разбор с
    фильтром ``sent`` вернул бы всю историю отправок и ничего не сказал бы о
    застрявшем. Опечатка в состоянии отсекается на границе, а не отдаёт
    молчаливо пустой список.
    """

    FAILED = OutboxState.FAILED.value
    SENDING = OutboxState.SENDING.value


class AddAccountRequest(BaseModel):
    phone: str = Field(min_length=3, max_length=32)
    label: str | None = Field(default=None, max_length=MAX_LABEL_LENGTH)


class AccountRequest(BaseModel):
    account_id: int


class LoginCompleteRequest(BaseModel):
    challenge_id: str = Field(max_length=128)
    code: str = Field(max_length=32)


class ChallengeRequest(BaseModel):
    challenge_id: str = Field(max_length=128)


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


def http_error(status_code: int, exc: Exception) -> HTTPException:
    return HTTPException(status_code=status_code, detail=str(exc))
