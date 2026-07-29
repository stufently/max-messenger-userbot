"""Доменные модели. Не зависят ни от транспорта, ни от FastAPI, ни от Typer."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field

from maxub.core.permissions import Scope


def utcnow() -> datetime:
    return datetime.now(tz=UTC)


class AccountState(StrEnum):
    """Состояния жизненного цикла аккаунта.

    Порядок нормального пути: NEW → AUTH_REQUIRED → CONNECTING → SYNCING → READY.
    BACKOFF — временная пауза после ошибок, DISABLED — остановлен вручную или
    после подозрения на блокировку.
    """

    NEW = "new"
    AUTH_REQUIRED = "auth_required"
    CONNECTING = "connecting"
    SYNCING = "syncing"
    READY = "ready"
    BACKOFF = "backoff"
    DISABLED = "disabled"


class OutboxState(StrEnum):
    """Состояния записи в очереди отправки.

    CLAIMED и SENDING разделены намеренно. CLAIMED — «воркер забрал запись
    себе, но транспорт её ещё не видел», SENDING — «сетевой вызов начат, исход
    может быть любым». Если бы состояние было одно, после падения процесса вся
    захваченная пачка выглядела бы неоднозначной, хотя дальше первой записи
    дело не дошло: остальные пришлось бы отдавать человеку вместо простого
    повтора.

    DISCARDED отделён от FAILED по той же причине: FAILED — «отправить не
    получилось, решение ещё не принято», DISCARDED — «человек разобрал запись и
    решил не отправлять». Смешать их значило бы потерять единственный признак,
    по которому видно, ждёт запись разбора или он уже закончен, — и в списке
    застрявшего, и в статистике очереди.
    """

    QUEUED = "queued"
    CLAIMED = "claimed"
    SENDING = "sending"
    SENT = "sent"
    FAILED = "failed"
    DISCARDED = "discarded"


class Account(BaseModel):
    id: int
    phone: str
    label: str | None = None
    state: AccountState = AccountState.NEW
    last_error: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class OutboxItem(BaseModel):
    id: int
    account_id: int
    chat_id: str
    text: str
    idempotency_key: str
    state: OutboxState = OutboxState.QUEUED
    attempts: int = 0
    remote_message_id: str | None = None
    error: str | None = None
    # Причина отказа лежит отдельно от `error`: там записано, почему отправка не
    # получилась, и при разборе спустя месяцы это нужно ровно так же, как и
    # решение человека. Одно поле на двоих означало бы, что отказ стирает улику.
    discard_reason: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    claimed_at: datetime | None = None
    next_attempt_at: datetime | None = None
    sent_at: datetime | None = None
    discarded_at: datetime | None = None


class Message(BaseModel):
    """Нормализованное сообщение — общий вид для любого транспорта."""

    remote_id: str
    chat_id: str
    sender_id: str | None = None
    text: str | None = None
    outgoing: bool = False
    timestamp: datetime = Field(default_factory=utcnow)


class Event(BaseModel):
    """Событие ядра. `dedup_key` защищает от повторов после reconnect."""

    account_id: int | None
    kind: str
    payload: dict[str, object] = Field(default_factory=dict)
    dedup_key: str
    created_at: datetime = Field(default_factory=utcnow)


class LoginChallenge(BaseModel):
    """Запрос кода подтверждения при входе по телефону."""

    challenge_id: str
    phone: str
    expires_at: datetime


class QrStatus(StrEnum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    EXPIRED = "expired"


class QrChallenge(BaseModel):
    """Запрос входа по QR-коду.

    ``payload`` — это содержимое, которое кодируется в QR и сканируется
    приложением MAX на телефоне. Номер телефона при таком входе не нужен.
    """

    challenge_id: str
    payload: str
    expires_at: datetime


class ApiToken(BaseModel):
    """Выпущенный токен API — всё, кроме самого секрета.

    Секрета здесь нет и в базе его нет: хранится только отпечаток. Показать
    токен второй раз демон не сможет даже по требованию владельца, и это
    осознанно — украденная копия базы не даёт доступа к API.
    """

    id: int
    label: str
    scopes: frozenset[Scope]
    created_at: datetime = Field(default_factory=utcnow)
    expires_at: datetime | None = None
    last_used_at: datetime | None = None
    revoked_at: datetime | None = None

    def is_active(self, now: datetime | None = None) -> bool:
        """Годен ли токен прямо сейчас: не отозван и не просрочен."""
        if self.revoked_at is not None:
            return False
        return self.expires_at is None or self.expires_at > (now or utcnow())


class Session(BaseModel):
    """Авторизационные данные аккаунта.

    В v1 хранится как есть; шифрование at rest с ключом вне SQLite — отдельная
    задача, см. TASKS.md.
    """

    account_id: int
    phone: str
    token: str
    device_id: str
