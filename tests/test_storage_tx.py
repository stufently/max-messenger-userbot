"""Транзакции хранилища: одно соединение, много корутин.

Соединение с БД одно на весь демон, и до общего замка записи любая корутина
могла зафиксировать чужую наполовину сделанную работу своим ``commit``. Тесты
проверяют не отдельные методы, а именно это свойство: составная операция либо
видна целиком, либо не видна вовсе, сколько бы соседей ни писало рядом.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from maxub.config import Settings
from maxub.core.crypto import SecretBox
from maxub.core.models import AccountState, Event, OutboxItem, OutboxState
from maxub.core.storage import Storage

# Сколько раз отдать управление циклу, изображая ``await`` между операторами.
# Одного оборота мало: соседней корутине нужно успеть дойти до своего commit.
YIELDS = 10

# Ни один тест здесь не имеет права ждать: зависание — это и есть искомый
# дедлок, а не медленная база.
TIMEOUT = 5.0


@pytest.fixture
async def storage(settings: Settings) -> AsyncIterator[Storage]:
    store = Storage(settings.db_path, SecretBox(settings.resolve_secret_key()))
    await store.open()
    try:
        yield store
    finally:
        await store.close()


async def _account(storage: Storage) -> int:
    account = await storage.add_account("+79990000001", None)
    return account.id


async def _sending(storage: Storage, account_id: int, key: str) -> OutboxItem:
    """Ставит сообщение в очередь и доводит до состояния «в полёте»."""
    item, _ = await storage.enqueue(account_id, "chat", "текст", key, 60.0)
    await storage.claim_queued()
    assert await storage.mark_sending(item.id)
    return item


def _event(account_id: int, key: str) -> Event:
    return Event(account_id=account_id, kind="message.sent", payload={}, dedup_key=key)


async def _yield_loop() -> None:
    for _ in range(YIELDS):
        await asyncio.sleep(0)


async def test_failed_event_rolls_back_sent_mark(
    storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Отметка об отправке не переживает сбой записи события.

    Именно этот сценарий разъезжался без замка: соседний писатель фиксировал
    транзакцию в тот момент, когда в ней уже лежал ``UPDATE ... state='sent'``,
    но события ещё не было. Дальше сбой — и сообщение навсегда отправлено без
    события, то есть подписчик о нём никогда не узнает.
    """
    account_id = await _account(storage)
    item = await _sending(storage, account_id, "ключ-в-полёте")
    noise, _ = await storage.enqueue(account_id, "chat", "шум", "ключ-шума", 60.0)

    async def failing_insert(event: Event) -> bool:
        await _yield_loop()
        raise RuntimeError("сбой записи события")

    monkeypatch.setattr(storage, "insert_event", failing_insert)

    stop = asyncio.Event()

    async def noisy_writer() -> None:
        """Сосед, который всё это время активно пишет и фиксирует."""
        while not stop.is_set():
            await storage.mark_failed(noise.id, "боль")
            await storage.requeue(noise.id)

    writer = asyncio.create_task(noisy_writer())
    try:
        with pytest.raises(RuntimeError):
            await asyncio.wait_for(
                storage.mark_sent_with_event(
                    item.id, "remote-1", _event(account_id, "ключ-события")
                ),
                TIMEOUT,
            )
    finally:
        stop.set()
        await asyncio.wait_for(writer, TIMEOUT)

    stored = await storage.get_outbox(item.id)
    assert stored is not None
    assert stored.state is OutboxState.SENDING, "отметка об отправке пережила сбой события"
    assert stored.remote_message_id is None
    assert [event for _, event in await storage.list_events() if event.kind == "message.sent"] == []


async def test_rollback_keeps_neighbour_writes(
    storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Откат забирает только свою работу, а не всё, что накопилось рядом.

    Проверка обратной стороны замка: раз ``rollback`` отменяет всё незакрытое
    на соединении, писатель не должен получать управление внутри чужой
    транзакции — иначе неудача одного стирала бы записи другого.
    """
    account_id = await _account(storage)
    item = await _sending(storage, account_id, "ключ-в-полёте")

    async def failing_insert(event: Event) -> bool:
        await _yield_loop()
        raise RuntimeError("сбой записи события")

    monkeypatch.setattr(storage, "insert_event", failing_insert)

    async def neighbour() -> None:
        await storage.save_cursor(account_id, "позиция-соседа")
        await storage.save_penalty(account_id, "send", item.created_at)

    together = asyncio.gather(
        storage.mark_sent_with_event(item.id, "remote-1", _event(account_id, "ключ-события")),
        neighbour(),
        return_exceptions=True,
    )
    results = await asyncio.wait_for(together, TIMEOUT)
    assert isinstance(results[0], RuntimeError)

    assert await storage.load_cursor(account_id) == "позиция-соседа"
    assert len(await storage.load_penalties()) == 1


async def test_nested_writes_do_not_deadlock(storage: Storage) -> None:
    """Составные операции не вешаются на собственном замке.

    ``mark_sent_with_event`` пишет событие через ``insert_event``, ``enqueue``
    перечитывает очередь по ключу — обычный ``asyncio.Lock`` не реентерабелен,
    и вложенный захват остановил бы демон навсегда. Тест ловит ровно это:
    зависание здесь означает не медленную базу, а взаимную блокировку.
    """
    account_id = await _account(storage)
    item = await _sending(storage, account_id, "ключ-в-полёте")

    published = await asyncio.wait_for(
        storage.mark_sent_with_event(item.id, "remote-1", _event(account_id, "ключ-события")),
        TIMEOUT,
    )
    assert published

    # Нулевое окно дедупликации уводит enqueue в ветку разбора коллизии: там
    # между двумя вставками есть чтение по ключу, то есть вложенный вызов.
    first, created = await asyncio.wait_for(
        storage.enqueue(account_id, "chat", "повтор", "ключ-повтора", 0.0), TIMEOUT
    )
    assert created
    second, created_again = await asyncio.wait_for(
        storage.enqueue(account_id, "chat", "повтор", "ключ-повтора", 0.0), TIMEOUT
    )
    assert created_again
    assert second.id != first.id

    # Замок отпущен, а не удерживается после составной операции.
    await asyncio.wait_for(storage.record_event(_event(account_id, "ключ-после")), TIMEOUT)


async def test_nested_write_joins_outer_transaction(storage: Storage) -> None:
    """Вложенный блок записи — часть внешнего, а не самостоятельная операция.

    Составные операции собираются из готовых методов, и каждый из них берёт тот
    же замок. Непереходимый ``asyncio.Lock`` подвесил бы такую сборку навсегда,
    а вложенная фиксация вернула бы исходную беду: внешняя операция сорвалась,
    а её половина уже в базе. Проверяется и то, и другое: срыв на выходе не
    должен оставить ни одной строки.
    """
    account_id = await _account(storage)

    async def nested() -> None:
        async with storage.write() as db:
            await db.execute(
                "UPDATE accounts SET label = ? WHERE id = ?", ("вложенная-метка", account_id)
            )
            await storage.set_account_state(account_id, AccountState.READY)
            await storage.record_event(_event(account_id, "вложенное-событие"))
            raise RuntimeError("сбой после вложенной записи")

    with pytest.raises(RuntimeError):
        await asyncio.wait_for(nested(), TIMEOUT)

    account = await storage.get_account(account_id)
    assert account is not None
    assert account.label is None
    assert account.state is AccountState.AUTH_REQUIRED
    assert await storage.list_events() == []


async def test_parallel_writes_are_not_lost(storage: Storage) -> None:
    """Одновременные изменения из разных корутин не теряются и не смешиваются."""
    account_id = await _account(storage)
    total = 20

    async def one(index: int) -> None:
        item, created = await storage.enqueue(
            account_id, f"chat-{index}", f"текст-{index}", f"ключ-{index}", 60.0
        )
        assert created
        await storage.record_event(_event(account_id, f"событие-{index}"))
        await storage.mark_failed(item.id, f"ошибка-{index}")

    await asyncio.wait_for(asyncio.gather(*(one(index) for index in range(total))), TIMEOUT)

    stats = await storage.outbox_stats()
    assert stats[OutboxState.FAILED.value] == total
    assert len(await storage.list_events(limit=total * 2)) == total
    for index in range(total):
        stored = await storage.get_outbox_by_key(f"ключ-{index}")
        assert stored is not None
        assert stored.chat_id == f"chat-{index}"
        assert stored.error == f"ошибка-{index}"


async def test_parallel_sends_keep_event_per_message(storage: Storage) -> None:
    """Под нагрузкой у каждой отправки остаётся ровно своё событие."""
    account_id = await _account(storage)
    total = 15
    items = [await _sending(storage, account_id, f"полёт-{index}") for index in range(total)]

    await asyncio.wait_for(
        asyncio.gather(
            *(
                storage.mark_sent_with_event(
                    item.id, f"remote-{index}", _event(account_id, f"отправлено-{index}")
                )
                for index, item in enumerate(items)
            )
        ),
        TIMEOUT,
    )

    stats = await storage.outbox_stats()
    assert stats[OutboxState.SENT.value] == total
    keys = {event.dedup_key for _, event in await storage.list_events(limit=total * 2)}
    assert keys == {f"отправлено-{index}" for index in range(total)}
    for index, item in enumerate(items):
        stored = await storage.get_outbox(item.id)
        assert stored is not None
        assert stored.remote_message_id == f"remote-{index}"
