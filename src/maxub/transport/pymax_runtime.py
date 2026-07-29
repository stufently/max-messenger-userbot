"""Фоновая жизнь одного клиента PyMax.

`client.start()` у PyMax — это не «подключиться», а «работать»: он проходит
авторизацию и дальше висит до закрытия соединения. Контракту транспорта нужен
обратный порядок — вызовы возвращают управление, а поток событий существует
сам по себе. Здесь эта разница и переворачивается: `start()` живёт в фоновой
задаче, а её смерть становится обычным исключением там, где ядро его ждёт.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime
from typing import Any

from maxub.core.models import Message, utcnow
from maxub.transport.base import TransportOutcomeUnknown, Update
from maxub.transport.pymax_errors import translate
from maxub.transport.pymax_session import MemorySessionStore

#: Сколько ждать сетевого закрытия, прежде чем снимать задачу силой.
CLOSE_WAIT = 10.0


class ClientRuntime:
    """Владеет клиентом PyMax, его фоновой задачей и потоком событий."""

    def __init__(self) -> None:
        self.store = MemorySessionStore()
        self.client: Any | None = None
        self.extra: Any | None = None
        self.started = asyncio.Event()
        self.failure: BaseException | None = None
        self.updates: asyncio.Queue[Update] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None

    # --- запуск и остановка -------------------------------------------------

    def launch(self, client: Any) -> None:
        """Подписывается на события и уводит `client.start()` в фон."""
        client.on_start()(self._on_start)
        client.on_message()(self._on_message)
        self.client = client
        self._task = asyncio.create_task(self._run(client))

    async def _run(self, client: Any) -> None:
        try:
            await client.start()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Исключение оседает здесь, а не всплывает в никуда: разложить его
            # по таксономии сможет тот вызов, который в этот момент ждёт.
            self.failure = exc
        finally:
            # Разбудить ожидающих обязательно и при неудаче, иначе вход или
            # подключение будут ждать событие, которого уже не будет.
            self.started.set()

    async def close(self) -> None:
        """Закрывает клиент и снимает фоновую задачу.

        Сетевое закрытие ограничено по времени и обёрнуто в `finally`: зависший
        или упавший `close()` не должен оставлять задачу жить — она держит
        соединение с MAX, а транспорт уже считает себя отключённым.
        """
        client, task = self.client, self._task
        self.client, self._task = None, None
        try:
            if client is not None:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(client.close(), CLOSE_WAIT)
        finally:
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task

    @property
    def task(self) -> asyncio.Task[None] | None:
        return self._task

    @property
    def alive(self) -> bool:
        return self._task is not None and not self._task.done()

    # --- ожидание -----------------------------------------------------------

    async def await_event(
        self, event: asyncio.Event, wait_seconds: float, *, during_auth: bool = False
    ) -> None:
        """Ждёт события, помня, что фоновая задача может умереть раньше него.

        Ошибка задачи проверяется до самого события: `_run` взводит `started`
        и при неудаче, и без этой проверки провалившийся вход выглядел бы
        успешным.
        """
        waiter = asyncio.ensure_future(event.wait())
        watched: set[asyncio.Future[Any]] = {waiter}
        if self._task is not None:
            watched.add(self._task)
        try:
            await asyncio.wait(watched, timeout=wait_seconds, return_when=asyncio.FIRST_COMPLETED)
        finally:
            waiter.cancel()
        if self.failure is not None:
            raise translate(self.failure, during_auth=during_auth)
        if event.is_set():
            return
        raise TransportOutcomeUnknown(f"PyMax не ответил за {wait_seconds:.0f} с")

    async def next_update(self) -> Update | None:
        """Отдаёт очередное событие; ``None`` означает закрытое соединение.

        Просто ждать очередь нельзя: живой поток — единственный признак, по
        которому надзор ядра узнаёт об обрыве. Пока итератор молча висит на
        пустой очереди, аккаунт числится готовым, хотя соединения уже нет.
        """
        while True:
            if not self.updates.empty():
                return self.updates.get_nowait()
            task = self._task
            if task is None or task.done():
                if self.failure is not None:
                    raise translate(self.failure)
                return None
            getter = asyncio.ensure_future(self.updates.get())
            try:
                await asyncio.wait({getter, task}, return_when=asyncio.FIRST_COMPLETED)
            except BaseException:
                # Нас сняли ровно в тот момент, когда событие уже вынуто из
                # очереди: вернуть его обязательно, иначе сообщение исчезнет
                # молча — ядро о нём даже не узнает.
                if getter.done() and not getter.cancelled():
                    self.updates.put_nowait(getter.result())
                getter.cancel()
                raise
            if getter.done():
                return getter.result()
            # Задача умерла раньше события. Снятое ожидание доводится до конца
            # здесь же: `Queue.get` при отмене возвращает элемент обратно, и
            # следующий проход цикла подберёт его из очереди, ничего не теряя.
            getter.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await getter

    # --- события ------------------------------------------------------------

    async def _on_start(self, client: Any) -> None:
        self.started.set()

    async def _on_message(self, raw: Any, client: Any) -> None:
        message = self.to_message(raw)
        if message is not None:
            # Позиция в потоке — `None`: серверной метки PyMax не сообщает, а
            # подставить сюда идентификатор сообщения значило бы увести курсор
            # ядра в чужое пространство значений.
            self.updates.put_nowait(Update(message=message, cursor=None))

    def to_message(self, raw: Any) -> Message | None:
        """Приводит сообщение PyMax к доменному виду.

        ``None`` — для событий без чата: MAX присылает разные payload-ы, и
        сообщение без `chat_id` ядру некуда положить.
        """
        chat_id = getattr(raw, "chat_id", None)
        remote_id = getattr(raw, "id", None)
        if chat_id is None or remote_id is None:
            return None
        sender = getattr(raw, "sender", None)
        return Message(
            remote_id=str(remote_id),
            chat_id=str(chat_id),
            sender_id=None if sender is None else str(sender),
            text=getattr(raw, "text", None) or None,
            outgoing=sender is not None and sender == self.self_id(),
            timestamp=_moment(getattr(raw, "time", None)),
        )

    def self_id(self) -> int | None:
        """Идентификатор своего аккаунта — по нему отличается исходящее."""
        contact = getattr(getattr(self.client, "me", None), "contact", None)
        ident = getattr(contact, "id", None)
        return ident if isinstance(ident, int) else None

    def user_agent_dump(self) -> dict[str, Any] | None:
        agent = getattr(self.extra, "user_agent", None)
        if agent is None:
            return None
        try:
            dumped = agent.model_dump(mode="json")
        except Exception:
            # Отпечаток устройства не критичен для входа: без него подключение
            # состоится, просто с заново сгенерированным устройством.
            return None
        return dumped if isinstance(dumped, dict) else None


def _moment(raw: Any) -> datetime:
    """Время MAX приходит в миллисекундах; мусор заменяется текущим моментом."""
    if isinstance(raw, int | float) and not isinstance(raw, bool) and raw > 0:
        with contextlib.suppress(OverflowError, OSError, ValueError):
            return datetime.fromtimestamp(raw / 1000, tz=UTC)
    return utcnow()
