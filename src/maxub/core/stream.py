"""Живой поток событий аккаунта: подписка, добор и продвижение позиции.

Вынесен из [sync][maxub.core.sync] отдельно: там разговор про владение
транспортами и состояния аккаунта, здесь — про единственный порядок действий
«подписались, догнали пропущенное, только потом объявили готовым» и про то,
чем этот порядок подтверждается.

Ключевое здесь — что считать состоявшейся подпиской. `events()` — асинхронный
генератор: пока у него не запросили первый элемент, его тело не выполняется, и
на сервере подписки нет. Живая задача сама по себе не доказывает ничего, а
мёртвая задача во время добора означает, что подключение не удалось, сколько бы
успешным ни был добор.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable, Coroutine
from typing import Any

from maxub.core.backfill import received_event
from maxub.core.ports import AccountRepository, EventSink
from maxub.core.supervisor import StreamEnded
from maxub.transport.base import Transport, Update

#: Догоняет пропущенное с сохранённой позиции. Что именно добирается, потоку
#: знать незачем — ему важен только момент, когда добор закончился.
Backfill = Callable[[], Coroutine[Any, Any, Any]]


async def open_stream(
    account_id: int,
    transport: Transport,
    repo: AccountRepository,
    emit: EventSink,
    backfill: Backfill,
) -> asyncio.Task[None]:
    """Подписывается на живой поток, добирает пропущенное и отдаёт задачу потока.

    Подписка идёт ДО добора: между последним `fetch_updates` и подпиской иначе
    остаётся окно, и пришедшее в нём событие теряется, если транспорт не
    буферизует (заглушка буферизует, реальный сервер — вряд ли). Порции при
    таком порядке пересекаются, но это безопасно: журнал отбрасывает повтор по
    `dedup_key`.

    Пока добор не закончен, живой поток не двигает сохранённую позицию — иначе
    добор потерял бы своё место и пропущенное осталось бы недобранным. Позиции
    живых событий из этого окна не теряются: добор заканчивается лишь тогда,
    когда догнал текущий момент, то есть перешагнул через них.

    Неуспехом считается не только ошибка добора: поток мог умереть, пока добор
    шёл, — тогда подключение неуспешно, даже если догнать удалось. Возврат
    задачи означает ровно одно: подписка открыта и на момент возврата жива.
    """
    backfilled = asyncio.Event()
    subscribed = asyncio.Event()
    pump = asyncio.create_task(_pump(account_id, transport, repo, emit, backfilled, subscribed))
    try:
        await _wait_subscribed(pump, subscribed)
        await _backfill_while_alive(backfill, pump)
    except BaseException:
        # Брошенная задача пережила бы неудачную попытку, и после
        # переподключения на аккаунте оказались бы две подписки разом.
        await stop(pump)
        raise
    finally:
        backfilled.set()
    return pump


async def stop(task: asyncio.Task[None]) -> None:
    """Снимает задачу и дожидается её.

    Дожидается намеренно: отменённый поток иначе успевает записать курсор уже
    после того, как вызвавший закрыл транспорт и базу.
    """
    task.cancel()
    with contextlib.suppress(Exception, asyncio.CancelledError):
        await task


async def _pump(
    account_id: int,
    transport: Transport,
    repo: AccountRepository,
    emit: EventSink,
    backfilled: asyncio.Event,
    subscribed: asyncio.Event,
) -> None:
    """Слушает живой поток и продвигает позицию.

    Первое событие запрашивается отдельно от цикла: тело генератора не
    выполняется, пока не запросили первый элемент, поэтому «подписка открыта»
    объявляется только после того, как запрос ушёл в работу. Ждать самого
    события для этого нельзя — его может не быть часами.
    """
    stream = aiter(transport.events())
    first = asyncio.ensure_future(anext(stream))
    try:
        # timeout=0 — ровно один проход планировщика: тела генератора хватает,
        # чтобы дойти до ожидания событий или упасть на подписке.
        await asyncio.wait({first}, timeout=0)
        subscribed.set()
        update = await first
    except StopAsyncIteration:
        return
    except BaseException:
        # Отмена ожидания сама по себе запрос не снимает: за пределами этой
        # задачи он остался бы висеть на транспорте.
        first.cancel()
        raise
    await _consume(account_id, update, repo, emit, backfilled)
    async for update in stream:
        await _consume(account_id, update, repo, emit, backfilled)


async def _consume(
    account_id: int,
    update: Update,
    repo: AccountRepository,
    emit: EventSink,
    backfilled: asyncio.Event,
) -> None:
    """Публикует событие и при необходимости двигает позицию.

    Сохраняется именно `update.cursor` — позиция в потоке сервера, а не
    идентификатор сообщения: это разные пространства значений, и подмена увела
    бы следующий добор в никуда. `None` означает «транспорт позицию не
    сообщил», тогда курсор остаётся на прежнем месте и хвост будет добран
    заново.
    """
    await emit(received_event(account_id, update.message))
    if update.cursor is None or not backfilled.is_set():
        return
    await repo.save_cursor(account_id, update.cursor)


async def _wait_subscribed(pump: asyncio.Task[None], subscribed: asyncio.Event) -> None:
    """Ждёт подтверждения подписки — либо смерти потока вместо него."""
    waiter = asyncio.ensure_future(subscribed.wait())
    try:
        await asyncio.wait({waiter, pump}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        waiter.cancel()
    _require_alive(pump)


async def _backfill_while_alive(backfill: Backfill, pump: asyncio.Task[None]) -> None:
    """Добирает пропущенное, не спуская глаз с живого потока.

    Худший случай — поток умирает сразу после последней пустой страницы: добор
    честно догнал текущий момент, аккаунт объявляется готовым, а слушать сервер
    уже некому, и всё пришедшее до того, как надзор заметит обрыв, попадает
    ровно в то окно потери, которое добор и закрывает. Поэтому обе задачи
    ждутся вместе, без опроса в цикле, и смерть потока перевешивает удачный
    добор.
    """
    task = asyncio.create_task(backfill())
    try:
        await asyncio.wait({task, pump}, return_when=asyncio.FIRST_COMPLETED)
        _require_alive(pump)
    except BaseException:
        await stop(task)
        raise
    # Поток жив, значит закончился именно добор: его ошибка — здесь.
    await task


def _require_alive(pump: asyncio.Task[None]) -> None:
    """Проверяет, что поток всё ещё слушает сервер.

    Причина смерти пробрасывается как есть: `TransportAuthError` из потока
    обязан дойти до надзора неизменным, иначе отозванная сессия попадёт в
    бесконечные попытки вместо `AUTH_REQUIRED`.
    """
    if not pump.done():
        return
    error = None if pump.cancelled() else pump.exception()
    raise error or StreamEnded("живой поток закончился до готовности аккаунта")
