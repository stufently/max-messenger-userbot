"""Уборка журнала событий.

Журнал растёт всё время, пока демон жив, и сам не сжимается: строкой становится
каждое входящее сообщение, каждая отправка и каждая смена состояния аккаунта.
Демон рассчитан на месяцы непрерывной работы, а читают журнал только вперёд —
панель показывает последние события, клиенты идут по курсору `after_id`. Хвост
годичной давности не нужен никому и остаётся только весом файла.

Очередь отправки при этом не чистится, и это решение, а не забывчивость.
Дедупликация по явному `nonce` бессрочна по контракту `enqueue_message`: клиент
вправе прислать ту же заявку спустя месяц и рассчитывать, что второго сообщения
не будет. Удаление старой строки тихо отменило бы это обещание — а незаметно
нарушенная идемпотентность стоит дороже занятого места. Записи, ждущие ручного
разбора, не удаляются тем более.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from datetime import timedelta

from maxub.core.models import utcnow
from maxub.core.ports import EventJournal

#: Самая отстающая позиция обработчиков: дальше неё уборка не заходит.
#: ``None`` — обработчиков нет, журнал принадлежит только читателям.
CursorFloor = Callable[[], Awaitable[int | None]]

log = logging.getLogger(__name__)


class Housekeeper:
    """Периодически подрезает журнал событий."""

    def __init__(
        self,
        storage: EventJournal,
        retention_days: int,
        interval_seconds: float,
        floor: CursorFloor | None = None,
    ) -> None:
        self._storage = storage
        self._retention_days = retention_days
        self._interval = interval_seconds
        self._floor = floor
        self._stopping = asyncio.Event()

    def stop(self) -> None:
        self._stopping.set()

    async def run(self) -> None:
        """Чистит сразу и потом раз в интервал.

        Сразу — потому что демон могли не запускать неделями, и ждать ещё сутки
        до первой уборки незачем. Ожидание идёт через событие остановки, а не
        через сон: иначе остановка демона упиралась бы в неснимаемые сутки сна.
        """
        while not self._stopping.is_set():
            await self._prune_once()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stopping.wait(), timeout=self._interval)

    async def _prune_once(self) -> None:
        if self._retention_days <= 0:
            return
        older_than = utcnow() - timedelta(days=self._retention_days)
        try:
            # Граница обработчиков берётся перед самой уборкой, а не при сборке:
            # курсоры двигаются всё время работы демона, а уборка происходит раз
            # в сутки — значение, взятое заранее, к этому моменту устареет.
            keep_from_id = await self._floor() if self._floor is not None else None
            removed = await self._storage.prune_events(older_than, keep_from_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Уборка — не то, ради чего стоит ронять демон: journal переживёт
            # пропущенный проход, а следующий состоится через интервал.
            log.exception("не удалось подрезать журнал событий")
            return
        if removed:
            log.info("из журнала удалено %s событий старше %s дней", removed, self._retention_days)
