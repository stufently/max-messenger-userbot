"""Раздача событий обработчикам.

Диспетчер читает журнал по курсору каждого обработчика, отдаёт событие и
двигает курсор — в этом порядке. Отсюда гарантия «хотя бы один раз»: событие,
обработанное перед самым падением процесса, придёт снова, потому что курсор
сдвинуться не успел. Обратный порядок дал бы «не более одного раза», то есть
тихую потерю работы, а это хуже повтора: повтор гасится идемпотентностью
отправки, потеря не гасится ничем.

Ошибка обработчика не двигает курсор сразу. Событие повторяется до
``max_attempts`` раз, и только потом откладывается с записью `handler.failed` в
журнал. Обе крайности плохи: вечный повтор останавливает обработчика навсегда
на первом же неудобном событии, а пропуск с первой ошибки превращает заявленное
«хотя бы один раз» в «одну попытку и до свидания».
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Protocol

from maxub.core.handlers.contract import EventHandler, HandlerError
from maxub.core.handlers.registry import HandlerRegistry
from maxub.core.models import Event, utcnow
from maxub.core.ports import EventPublisher, HandlerJournal
from maxub.transport.base import Capabilities

log = logging.getLogger(__name__)

#: Сколько событий забирать из журнала за один заход.
BATCH_SIZE = 50

#: Пауза, когда журнал догнан. Служит и задержкой между попытками по событию,
#: которое обработчик пока не осилил.
IDLE_SECONDS = 1.0

#: Сколько раз подряд пробовать одно событие, прежде чем отложить его.
MAX_ATTEMPTS = 3

#: Приставка служебных событий самого диспетчера. Обработчикам они не
#: раздаются: обработчик с пустым `kinds` получал бы собственный `handler.failed`
#: и, продолжая падать, порождал бы на каждый отказ новый отказ — журнал рос бы
#: сам из себя. Читать эти события следует из журнала и API, как это делает
#: человек.
SERVICE_PREFIX = "handler."


class HandlerActions(Protocol):
    """Что диспетчер умеет сделать по просьбе обработчика."""

    async def enqueue(self, account_id: int, chat_id: str, text: str, nonce: str) -> int: ...

    def transport_capabilities(self, account_id: int) -> Capabilities | None: ...


class _Context:
    """Права обработчика на один разбор одного события.

    Аккаунт берётся из самого события и не выбирается обработчиком: иначе
    обработчик, откликнувшийся на сообщение одного аккаунта, мог бы написать от
    имени любого другого. Понадобится обратное — параметр добавится осознанно;
    убрать его потом было бы уже нельзя.
    """

    def __init__(
        self, actions: HandlerActions, handler_name: str, event_id: int, account_id: int | None
    ) -> None:
        self._actions = actions
        self._handler = handler_name
        self.event_id = event_id
        self._account_id = account_id
        self._sent = 0

    @property
    def account_id(self) -> int | None:
        return self._account_id

    async def send_text(self, chat_id: str, text: str) -> int:
        if self._account_id is None:
            raise HandlerError("событие не привязано к аккаунту: отправлять не от кого")
        # Порядковый номер внутри обработки — часть ключа: без него обработчик,
        # отправляющий на одно событие два разных сообщения, получил бы одну
        # запись, потому что второй вызов выглядел бы повтором первого.
        nonce = f"handler:{self._handler}:{self.event_id}:{self._sent}"
        self._sent += 1
        return await self._actions.enqueue(self._account_id, chat_id, text, nonce)

    def capabilities(self) -> Capabilities | None:
        if self._account_id is None:
            return None
        return self._actions.transport_capabilities(self._account_id)


class HandlerDispatcher:
    """Фоновый разбор журнала событий обработчиками."""

    def __init__(
        self,
        journal: HandlerJournal,
        registry: HandlerRegistry,
        actions: HandlerActions,
        publish: EventPublisher,
        wakeup: asyncio.Event | None = None,
        batch_size: int = BATCH_SIZE,
        idle_seconds: float = IDLE_SECONDS,
        max_attempts: int = MAX_ATTEMPTS,
    ) -> None:
        self._journal = journal
        self._registry = registry
        self._actions = actions
        self._publish = publish
        self._wakeup = wakeup or asyncio.Event()
        self._batch_size = batch_size
        self._idle = idle_seconds
        self._max_attempts = max_attempts
        self._stopping = asyncio.Event()

    # --- жизненный цикл -----------------------------------------------------

    async def prepare(self) -> None:
        """Заводит курсоры и сообщает о разрыве, если он случился.

        Новый обработчик начинает с конца журнала. Уже известный — с того места,
        где остановился; если уборка успела съесть часть его хвоста (обработчик
        сняли, журнал подрезали, обработчик вернули), об этом пишется
        `handler.gap`. Молчать здесь нельзя: пропуск выглядит как «событий не
        было», и разбираться потом будет не с чем.
        """
        if not self._registry:
            return
        latest = await self._journal.last_event_id()
        oldest = await self._journal.first_event_id()
        for handler in self._registry.handlers:
            known = await self._journal.load_handler_cursor(handler.name)
            cursor = await self._journal.init_handler_cursor(handler.name, latest)
            if known is None or oldest is None or cursor >= oldest - 1:
                continue
            await self._record(
                Event(
                    account_id=None,
                    kind="handler.gap",
                    payload={"handler": handler.name, "cursor": cursor, "oldest_event": oldest},
                    dedup_key=f"handler-gap:{handler.name}:{cursor}",
                )
            )

    async def run(self) -> None:
        """Разбирает журнал, пока не попросят остановиться."""
        if not self._registry:
            return
        while not self._stopping.is_set():
            try:
                progressed = await self._pass()
            except asyncio.CancelledError:
                raise
            except Exception:
                # Диспетчер не имеет права умереть от чужой ошибки: без него
                # обработчики молча перестанут получать события.
                log.exception("сбой разбора журнала обработчиками")
                progressed = False
            if progressed:
                continue
            self._wakeup.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wakeup.wait(), timeout=self._idle)

    def stop(self) -> None:
        self._stopping.set()
        self._wakeup.set()

    # --- разбор -------------------------------------------------------------

    async def _pass(self) -> bool:
        """Один заход по всем обработчикам. ``True`` — что-то продвинулось."""
        progressed = False
        for handler in self._registry.handlers:
            progressed |= await self._pump(handler)
        return progressed

    async def _pump(self, handler: EventHandler) -> bool:
        cursor = await self._journal.load_handler_cursor(handler.name)
        if cursor is None:
            return False
        batch = await self._journal.list_events(limit=self._batch_size, after_id=cursor)
        position = cursor
        for event_id, event in batch:
            if not await self._deliver(handler, position, event_id, event):
                # Событие не далось: остальную пачку не трогаем, иначе порядок
                # событий для обработчика перестал бы что-либо значить.
                return False
            position = event_id
        return bool(batch)

    async def _deliver(
        self, handler: EventHandler, position: int, event_id: int, event: Event
    ) -> bool:
        """Отдаёт одно событие. ``False`` — курсор остался на месте."""
        if event.kind.startswith(SERVICE_PREFIX) or not self._registry.wants(handler, event.kind):
            await self._journal.advance_handler_cursor(handler.name, event_id)
            return True
        blocked = await self._gate(handler, event)
        if blocked is not None:
            if await self._journal.advance_handler_cursor(handler.name, event_id, blocked):
                self._publish(blocked)
            return True
        try:
            context = _Context(self._actions, handler.name, event_id, event.account_id)
            await handler.handle(event, context)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return await self._failed(handler, position, event_id, event, exc)
        await self._journal.advance_handler_cursor(handler.name, event_id)
        return True

    async def _gate(self, handler: EventHandler, event: Event) -> Event | None:
        """Проверяет возможности транспорта; событие — причина отказа.

        Отсутствие соединения отказом не считается: возможности аккаунта в этот
        момент просто неизвестны, а действие всё равно упрётся в очередь отправки
        и объяснится там. Отказ — это точно известное «транспорт этого не умеет».
        """
        if not handler.requires:
            return None
        if event.account_id is None:
            return Event(
                account_id=None,
                kind="handler.skipped",
                payload={
                    "handler": handler.name,
                    "kind": event.kind,
                    "reason": "событие не привязано к аккаунту",
                },
                # Ключ без номера события: иначе каждое такое событие добавляло
                # бы в журнал строку о пропуске, и журнал состоял бы из них.
                dedup_key=f"handler-skip:{handler.name}:{event.kind}",
            )
        capabilities = self._actions.transport_capabilities(event.account_id)
        if capabilities is None:
            return None
        missing = sorted(
            name for name in handler.requires if not getattr(capabilities, name, False)
        )
        if not missing:
            return None
        return Event(
            account_id=event.account_id,
            kind="handler.capability_missing",
            payload={"handler": handler.name, "missing": missing},
            dedup_key=f"handler-cap:{handler.name}:{event.account_id}:{','.join(missing)}",
        )

    async def _failed(
        self, handler: EventHandler, position: int, event_id: int, event: Event, exc: Exception
    ) -> bool:
        attempts = await self._journal.bump_handler_attempts(handler.name, position)
        if attempts < self._max_attempts:
            log.warning(
                "обработчик %s не справился с событием %s (попытка %s): %s",
                handler.name,
                event_id,
                attempts,
                exc,
            )
            return False
        log.error(
            "обработчик %s отложил событие %s после %s попыток: %s",
            handler.name,
            event_id,
            attempts,
            exc,
        )
        failure = Event(
            account_id=event.account_id,
            kind="handler.failed",
            payload={
                "handler": handler.name,
                "event_id": event_id,
                "event_kind": event.kind,
                "attempts": attempts,
                "error": str(exc),
            },
            dedup_key=f"handler-failed:{handler.name}:{event_id}",
            created_at=utcnow(),
        )
        if await self._journal.advance_handler_cursor(handler.name, event_id, failure):
            self._publish(failure)
        return True

    async def _record(self, event: Event) -> None:
        """Пишет служебное событие, не трогая ничьих курсоров."""
        if await self._journal.record_event(event):
            self._publish(event)
