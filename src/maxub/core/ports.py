"""Порты ядра.

Ядро зависит от узких протоколов, а не от конкретных классов: так его части
тестируются по отдельности, а замена хранилища не требует правок логики.
Протоколы описывают ровно то, что вызывается, — не всю поверхность `Storage`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime
from typing import Any, Protocol

from maxub.core.models import Account, AccountState, Event, OutboxItem, OutboxState
from maxub.transport.base import Transport

TransportFactory = Callable[[], Transport]
EventSink = Callable[[Event], Awaitable[None]]

#: Записывает состояние аккаунта и рассказывает о нём подписчикам. Берётся
#: вместо прямого обращения к хранилищу везде, где состояние меняется по ходу
#: работы: событие и запись обязаны уходить вместе, а пара вызовов на каждом
#: переходе рано или поздно расходится.
AccountStateWriter = Callable[[int, AccountState, str | None], Awaitable[None]]

#: Раздача уже записанного события подписчикам. Отдельно от `EventSink`: там,
#: где событие пишется вместе с изменением в одной транзакции, повторная запись
#: не нужна — остаётся только раздача.
EventPublisher = Callable[[Event], None]


class EventJournal(Protocol):
    """Обслуживание журнала событий. Отдельно от чтения и записи: уборке нужен
    ровно один метод, и знать про остальное хранилище ей незачем."""

    async def prune_events(self, older_than: datetime) -> int: ...


class AccountRepository(Protocol):
    async def list_accounts(self) -> list[Account]: ...

    async def get_account(self, account_id: int) -> Account | None: ...

    async def set_account_state(
        self, account_id: int, state: AccountState, error: str | None = None
    ) -> None: ...

    async def set_account_state_with_event(
        self, account_id: int, state: AccountState, error: str | None, event: Event
    ) -> bool: ...

    async def save_session(self, account_id: int, payload: dict[str, Any]) -> None: ...

    async def load_session(self, account_id: int) -> dict[str, Any] | None: ...

    async def save_cursor(self, account_id: int, cursor_value: str) -> None: ...

    async def load_cursor(self, account_id: int) -> str | None: ...


class OutboxRepository(Protocol):
    async def claim_queued(self, limit: int = 10) -> list[OutboxItem]: ...

    async def mark_sending(self, item_id: int) -> bool: ...

    async def release_claimed(self, item_id: int) -> None: ...

    async def defer_claimed(self, item_id: int, until: datetime) -> None: ...

    async def release_all_claimed(self) -> int: ...

    async def list_stale_sending(self) -> list[OutboxItem]: ...

    async def schedule_retry(self, item_id: int, next_attempt_at: datetime) -> None: ...

    async def mark_sent_with_event(
        self, item_id: int, remote_message_id: str, event: Event
    ) -> bool: ...

    async def mark_failed(self, item_id: int, error: str) -> None: ...

    # Ручной разбор: человек смотрит, что зависло, и решает по конкретной записи.
    async def get_outbox(self, item_id: int) -> OutboxItem | None: ...

    async def list_outbox(self, states: Sequence[OutboxState], limit: int) -> list[OutboxItem]: ...

    async def requeue(self, item_id: int) -> bool: ...

    async def discard(self, item_id: int, reason: str, event: Event) -> bool: ...

    async def mark_sent_after_review(
        self, item_id: int, remote_message_id: str, event: Event
    ) -> bool: ...

    async def save_penalty(self, account_id: int, action: str, until: datetime) -> None: ...

    async def clear_penalty(self, account_id: int, action: str) -> None: ...
