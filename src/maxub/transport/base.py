"""Контракт транспорта.

Внутренний API MAX меняется без предупреждения, поэтому конкретная библиотека
(`PyMax`, `pyromax`) живёт за этим интерфейсом. Абстрагируются только те
возможности, которые нужны v1 — расширять по мере надобности, а не «на всякий
случай».
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from enum import StrEnum
from typing import Protocol, runtime_checkable

from pydantic import BaseModel

from maxub.core.models import LoginChallenge, Message, QrChallenge, Session


class Update(BaseModel):
    """Сообщение вместе с позицией в потоке сервера.

    Позиция обязана быть из того же пространства значений, что и ``cursor`` в
    [fetch_updates][maxub.transport.base.Transport.fetch_updates]: добор
    пропущенного и живой поток сохраняют её в одно и то же место, и подмена
    одного идентификатора другим (например, идентификатором сообщения) увела
    бы курсор в чужую систему координат.

    ``None`` означает «транспорт не сообщает позицию для этого события» — тогда
    курсор не двигается, а пропущенное добирается по прошлой позиции.
    """

    message: Message
    cursor: str | None = None


class ReconcileOutcome(StrEnum):
    """Чем закончилась сверка отправки с сервером.

    Различать «точно не дошло» и «выяснить не удалось» обязательно: повтор
    допустим только в первом случае, во втором он создаст дубль.
    """

    FOUND = "found"
    NOT_FOUND = "not_found"
    INCONCLUSIVE = "inconclusive"


class ReconcileResult(BaseModel):
    """Результат сверки. ``message`` заполнен только при ``FOUND``."""

    outcome: ReconcileOutcome
    message: Message | None = None
    detail: str | None = None


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

    #: Отдаёт события с сохранённой позиции — позволяет добрать пропущенное за
    #: время простоя, а не только слушать новое.
    backfill: bool = False

    #: Умеет найти отправленное сообщение по клиентскому токену. Без этого
    #: неоднозначную отправку невозможно разобрать автоматически.
    reconcile: bool = False

    #: Вход по QR-коду вместо номера телефона и кода из SMS.
    qr_login: bool = False


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

    async def start_qr_login(self) -> QrChallenge:
        """Начинает вход по QR-коду. Номер телефона при этом не нужен."""
        ...

    async def poll_qr_login(self, challenge_id: str, account_id: int) -> Session | None:
        """Проверяет, подтверждён ли вход с телефона.

        ``None`` означает «ещё не подтверждён»; истёкший запрос — это
        ``TransportAuthError``.
        """
        ...

    async def connect(self, session: Session) -> Session | None:
        """Подключается по сохранённой сессии.

        Возвращает обновлённую сессию, если сервер выдал её на этом входе, и
        ``None``, если прежняя осталась в силе. Без этого возврата ротация
        токена на стороне MAX означала бы, что после перезапуска демон приходит
        со старым токеном и просит человека войти заново: адаптер новый токен
        получил, а сообщить о нём ядру ему было нечем.
        """
        ...

    async def disconnect(self) -> None: ...

    async def send_text(self, chat_id: str, text: str, client_token: str) -> str:
        """Отправляет текст.

        ``client_token`` сопровождает сообщение на той стороне и позволяет
        позже опознать его — без этого после обрыва связи невозможно понять,
        дошло сообщение или нет.
        """
        ...

    async def fetch_history(self, chat_id: str, limit: int) -> list[Message]: ...

    async def fetch_updates(
        self, cursor: str | None, limit: int
    ) -> tuple[list[Update], str | None]:
        """Отдаёт события начиная с позиции ``cursor``.

        Возвращает порцию событий и новую позицию. Пустой список означает, что
        догнали текущий момент. Новая позиция обязана продвигаться, пока есть
        что отдавать: неподвижный курсор при непустой порции ядро трактует как
        неисправность транспорта и прекращает добор.
        """
        ...

    async def reconcile_send(self, chat_id: str, client_token: str) -> ReconcileResult:
        """Выясняет у сервера судьбу ранее отправленного сообщения.

        ``NOT_FOUND`` разрешено возвращать только тогда, когда отсутствие
        сообщения действительно доказано: поиск покрыл нужный промежуток
        времени, пагинация пройдена до конца и сервер не отдаёт результат с
        задержкой. Во всех остальных случаях — ``INCONCLUSIVE``: лучше отдать
        запись человеку, чем создать дубль.
        """
        ...

    def events(self) -> AsyncIterator[Update]: ...
