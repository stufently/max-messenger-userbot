"""Ручной разбор записей, которые платформа не решилась повторить сама.

Отделено от [сверки][maxub.core.reconcile] по границе ответственности: там
автоматика решает за человека и повторяет только доказанное «не дошло», здесь
решение принимает он сам и вправе повторить даже то, что выяснить не удалось.
Задача модуля — не решать за него, а сначала спросить сервер и честно назвать
риск.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from enum import StrEnum

from pydantic import BaseModel

from maxub.core.models import Message, OutboxItem, OutboxState
from maxub.core.ports import EventPublisher, OutboxRepository
from maxub.core.reconcile import sent_event
from maxub.transport.base import ReconcileOutcome, Transport

log = logging.getLogger(__name__)

DUPLICATE_WARNING = (
    "сверить с сервером не удалось: сообщение могло дойти, повтор способен создать дубль"
)


class RetryCheck(StrEnum):
    """Чем закончилась сверка перед ручным повтором.

    «Не смогли спросить» разбито на два значения намеренно: нет соединения —
    состояние временное и стоит повторить позже, неумение транспорта не пройдёт
    никогда. Человеку это говорит разное, а по одному слову «не удалось» он бы
    не отличил одно от другого.
    """

    FOUND = "found"
    NOT_FOUND = "not_found"
    INCONCLUSIVE = "inconclusive"
    NO_CONNECTION = "no_connection"
    UNSUPPORTED = "unsupported"


class ManualRetryResult(BaseModel):
    """Исход ручного разбора — одинаковый для человека и для скрипта.

    ``duplicate_risk`` выделен отдельным полем, а не оставлен в тексте: скрипту
    нужен признак, который можно проверить, а не строка, которую надо читать.
    """

    requeued: bool
    check: RetryCheck
    duplicate_risk: bool
    detail: str | None = None
    item: OutboxItem


class OutboxItemNotFound(Exception):
    """Записи с таким идентификатором нет."""


class OutboxItemBusy(Exception):
    """Запись не в терминальном состоянии — распоряжается ею не человек."""


class ManualRetry:
    """Разбор конкретной записи по решению человека."""

    def __init__(
        self,
        repo: OutboxRepository,
        get_transport: Callable[[int], Transport | None],
        publish: EventPublisher,
    ) -> None:
        self._repo = repo
        self._get_transport = get_transport
        self._publish = publish
        # Разборы идут по одному. Между чтением записи и решением по ней стоит
        # сетевая сверка, и без замка два одновременных повтора успели бы
        # принять по одной записи разные решения: первый закрыл бы её
        # доказанной доставкой, второй в это же время вернул бы в очередь — и
        # получатель увидел бы дубль. Проверки состояния в SQL от этого не
        # спасают: к моменту второго решения первое ещё не записано. Замок
        # процесса здесь достаточен — демон у базы один, а состояние
        # «разбирается» в самой записи потребовало бы миграции схемы. Ручной
        # разбор — редкое действие человека, очередь из таких команд не мешает.
        self._one_at_a_time = asyncio.Lock()

    async def retry(self, item_id: int) -> ManualRetryResult:
        """Повторяет отправку отказавшей записи.

        Сверка выполняется всегда, когда есть чем: она ничего не отправляет и
        может только уменьшить вред — доказанное «сообщение уже на сервере»
        закрывает запись без второй отправки. Смысла команды это не меняет:
        человек просит, чтобы сообщение дошло один раз, а не чтобы ушёл ещё один
        сетевой вызов. Исход сверки всегда возвращается наверх, поэтому молчания
        тут нет.
        """
        async with self._one_at_a_time:
            return await self._retry(item_id)

    async def _retry(self, item_id: int) -> ManualRetryResult:
        item = await self._repo.get_outbox(item_id)
        if item is None:
            raise OutboxItemNotFound(f"запись очереди {item_id} не найдена")
        if item.state is not OutboxState.FAILED:
            raise OutboxItemBusy(
                f"запись {item_id} в состоянии {item.state.value}:"
                " повтор разрешён только из failed, иначе её отправляет воркер"
            )
        check, message, detail = await self._check(item)
        if check is RetryCheck.FOUND and message is not None:
            return await self._close(item, message, detail)
        if not await self._repo.requeue(item.id):
            # Состояние сменилось, пока шла сверка: разобрать запись успел
            # кто-то другой, и второе решение по ней принимать нельзя.
            raise OutboxItemBusy(f"запись {item_id} разобрана параллельно, повтор отменён")
        return ManualRetryResult(
            requeued=True,
            check=check,
            duplicate_risk=check is not RetryCheck.NOT_FOUND,
            detail=detail if check is RetryCheck.NOT_FOUND else f"{DUPLICATE_WARNING} ({detail})",
            item=await self._reread(item),
        )

    async def _close(
        self, item: OutboxItem, message: Message, detail: str | None
    ) -> ManualRetryResult:
        """Закрывает запись, которую сервер всё-таки принял.

        Повторной отправки не будет: сообщение у получателя уже есть. Событие о
        нём публикуется, если раньше его никто не записал, — подписчик обязан
        увидеть отправку ровно один раз, даже если она подтвердилась спустя
        сутки и по просьбе человека.
        """
        event = sent_event(item, message.remote_id)
        if await self._repo.mark_sent_after_review(item.id, message.remote_id, event):
            self._publish(event)
        stored = await self._reread(item)
        if stored.state is not OutboxState.SENT:
            raise OutboxItemBusy(f"запись {item.id} разобрана параллельно, повтор отменён")
        return ManualRetryResult(
            requeued=False,
            check=RetryCheck.FOUND,
            duplicate_risk=False,
            detail=detail or "сообщение уже на сервере, повтор не нужен",
            item=stored,
        )

    async def _check(self, item: OutboxItem) -> tuple[RetryCheck, Message | None, str | None]:
        """Спрашивает сервер о судьбе сообщения, ничего не меняя в записи.

        Состояние не трогается намеренно: решение принимает человек, и до него
        запись должна дожить нетронутой, чем бы ни закончился запрос.
        """
        transport = self._get_transport(item.account_id)
        if transport is None:
            return RetryCheck.NO_CONNECTION, None, "нет активного соединения"
        if not transport.capabilities.reconcile:
            return RetryCheck.UNSUPPORTED, None, "транспорт не умеет сверять отправленное"
        try:
            result = await transport.reconcile_send(item.chat_id, item.idempotency_key)
        except Exception as exc:
            # Сбой сверки — это не отказ отправки: запись остаётся как была, а
            # человек получает тот же неизвестный исход, что и раньше.
            log.warning("не удалось сверить сообщение %s: %s", item.id, exc)
            return RetryCheck.INCONCLUSIVE, None, f"сверка не удалась ({exc})"
        if result.outcome is ReconcileOutcome.FOUND and result.message is not None:
            return RetryCheck.FOUND, result.message, None
        if result.outcome is ReconcileOutcome.NOT_FOUND:
            return RetryCheck.NOT_FOUND, None, "сервер подтвердил, что сообщения нет"
        return RetryCheck.INCONCLUSIVE, None, result.detail or "сверка не дала ответа"

    async def _reread(self, item: OutboxItem) -> OutboxItem:
        """Перечитывает запись, чтобы ответ показывал итог, а не исходник."""
        return await self._repo.get_outbox(item.id) or item
