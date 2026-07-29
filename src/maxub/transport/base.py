"""Контракт транспорта.

Внутренний API MAX меняется без предупреждения, поэтому конкретная библиотека
(`PyMax`, `pyromax`) живёт за этим интерфейсом. Абстрагируются только те
возможности, которые нужны v1 — расширять по мере надобности, а не «на всякий
случай».
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from pydantic import BaseModel

from maxub.core.models import LoginChallenge, Message, Session


class Capabilities(BaseModel):
    """Что умеет конкретный адаптер.

    Явный список нужен, чтобы отсутствие функции у второго адаптера не
    маскировалось молча.
    """

    send_text: bool = False
    fetch_history: bool = False
    edit_message: bool = False
    delete_message: bool = False
    media: bool = False


class TransportError(Exception):
    """Базовая ошибка транспорта."""


class TransportNotApplied(TransportError):
    """Действие точно не выполнено на той стороне — повтор безопасен.

    Сюда попадают только случаи, где это достоверно известно: отказ до отправки
    запроса, явный отказ сервера принять команду.
    """


class TransportRateLimited(TransportNotApplied):
    """Сервер отверг запрос по лимиту и подсказал, когда повторить."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class TransportOutcomeUnknown(TransportError):
    """Исход неизвестен: таймаут, обрыв соединения, неопознанный сбой.

    Повторять автоматически нельзя — сообщение могло уйти получателю, и повтор
    дал бы дубль. Разбирается вручную.
    """


class TransportAuthError(TransportError):
    """Сессия отозвана, требуется 2FA или повторный вход."""


class TransportPermanent(TransportError):
    """Запрос некорректен и не станет корректным при повторе."""


class TransportUnsupported(TransportError):
    """Возможность не поддерживается этим адаптером."""


@runtime_checkable
class Transport(Protocol):
    """Выходной адаптер к мессенджеру.

    Один экземпляр обслуживает один аккаунт: состояние аккаунтов изолировано.
    """

    name: str
    capabilities: Capabilities

    async def start_login(self, phone: str) -> LoginChallenge: ...

    async def complete_login(self, challenge_id: str, code: str, account_id: int) -> Session: ...

    async def connect(self, session: Session) -> None: ...

    async def disconnect(self) -> None: ...

    async def send_text(self, chat_id: str, text: str) -> str: ...

    async def fetch_history(self, chat_id: str, limit: int) -> list[Message]: ...

    def events(self) -> AsyncIterator[Message]: ...
