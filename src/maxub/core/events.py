"""Раздача событий подписчикам.

Событие сначала записывается в журнал и только потом уходит подписчикам: журнал
и есть источник правды, а подписка — лишь способ узнать о новом сразу.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from maxub.core.models import Event

log = logging.getLogger(__name__)

LISTENER_QUEUE_SIZE = 1000


class EventBus:
    def __init__(self, queue_size: int = LISTENER_QUEUE_SIZE) -> None:
        self._queue_size = queue_size
        self._listeners: list[asyncio.Queue[Event]] = []

    def publish(self, event: Event) -> None:
        for queue in list(self._listeners):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # Медленный подписчик не должен раздувать память демона:
                # пропущенное он дочитает из журнала по курсору.
                log.warning("подписчик не успевает читать события, событие отброшено")

    def subscribe(self) -> asyncio.Queue[Event]:
        queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=self._queue_size)
        self._listeners.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[Event]) -> None:
        with contextlib.suppress(ValueError):
            self._listeners.remove(queue)
