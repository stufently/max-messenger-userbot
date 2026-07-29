"""Раздача событий подписчикам.

Событие сначала записывается в журнал и только потом уходит подписчикам: журнал
и есть источник правды, а подписка — лишь способ узнать о новом сразу.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import logging

from maxub.core.models import AccountState, Event, utcnow

log = logging.getLogger(__name__)

LISTENER_QUEUE_SIZE = 1000

#: Состояния, о переходе в которые узнают подписчики. Промежуточных
#: `connecting` и `syncing` здесь нет намеренно: при каждом переподключении они
#: сменяются парами и в журнале дали бы шум, за которым потерялось бы главное.
#: Готовность объявляет отдельное событие `account.ready` — у него своя защита
#: от повторов, привязанная к соединению, а не ко времени.
NOTIFIED_STATES = frozenset(
    {AccountState.AUTH_REQUIRED, AccountState.BACKOFF, AccountState.DISABLED}
)

#: Счётчик переходов внутри запуска — часть ключа дедупликации, см. ниже.
_STATE_SEQUENCE = itertools.count()


def account_state_event(account_id: int, state: AccountState, error: str | None) -> Event | None:
    """Событие о смене состояния аккаунта; ``None`` — сообщать не о чем.

    Эти три состояния означают «дальше само не поедет»: нужен новый вход, идут
    попытки переподключиться или аккаунт выключен человеком. Раньше о них можно
    было узнать только опросом `/status` — то есть тот, кто подписался на живой
    поток, пропускал именно то, ради чего подписывался.

    Ключ дедупликации нарочно уникален: дедупликации здесь не место. Аккаунт
    может уходить в `backoff` и возвращаться сколько угодно раз, и каждый такой
    раз подписчику важен. Защита от повторов существует для событий, которые
    сервер выдаёт заново после переподключения, — а это событие рождается у нас.

    Одного времени для уникальности мало: разрешение системных часов в Windows
    измеряется миллисекундами, и два перехода подряд получили бы одну отметку —
    второй молча пропал бы на уникальном индексе журнала. Счётчик закрывает этот
    случай внутри запуска, отметка времени — между запусками, когда счётчик
    начинается заново.
    """
    if state not in NOTIFIED_STATES:
        return None
    mark = f"{utcnow().isoformat()}:{next(_STATE_SEQUENCE)}"
    return Event(
        account_id=account_id,
        kind=f"account.{state.value}",
        payload={"state": state.value, "error": error},
        dedup_key=f"account-state:{account_id}:{state.value}:{mark}",
    )


class EventBus:
    def __init__(self, queue_size: int = LISTENER_QUEUE_SIZE) -> None:
        self._queue_size = queue_size
        self._listeners: list[asyncio.Queue[Event]] = []
        self._signals: list[asyncio.Event] = []

    def publish(self, event: Event) -> None:
        for queue in list(self._listeners):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # Медленный подписчик не должен раздувать память демона:
                # пропущенное он дочитает из журнала по курсору.
                log.warning("подписчик не успевает читать события, событие отброшено")
        for signal in list(self._signals):
            signal.set()

    def subscribe(self) -> asyncio.Queue[Event]:
        queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=self._queue_size)
        self._listeners.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[Event]) -> None:
        with contextlib.suppress(ValueError):
            self._listeners.remove(queue)

    def signal(self) -> asyncio.Event:
        """Будильник вместо очереди: «что-то появилось», без самих событий.

        Нужен тому, кто читает журнал сам и от шины хочет только одного — не
        спать лишнюю секунду до следующего опроса. Очередь ему не подходит:
        она переполняется на всплеске и роняет в лог предупреждение о медленном
        подписчике, хотя терять здесь нечего — содержимое всё равно берётся из
        базы.
        """
        signal = asyncio.Event()
        self._signals.append(signal)
        return signal

    def drop_signal(self, signal: asyncio.Event) -> None:
        with contextlib.suppress(ValueError):
            self._signals.remove(signal)
