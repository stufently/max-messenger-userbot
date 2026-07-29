"""Доменные модели. Не зависят ни от транспорта, ни от FastAPI, ни от Typer."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field


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
    """

    QUEUED = "queued"
    CLAIMED = "claimed"
    SENDING = "sending"
    SENT = "sent"
    FAILED = "failed"


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
    created_at: datetime = Field(default_factory=utcnow)
    claimed_at: datetime | None = None
    next_attempt_at: datetime | None = None
    sent_at: datetime | None = None


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


class Session(BaseModel):
    """Авторизационные данные аккаунта.

    В v1 хранится как есть; шифрование at rest с ключом вне SQLite — отдельная
    задача, см. TASKS.md.
    """

    account_id: int
    phone: str
    token: str
    device_id: str
