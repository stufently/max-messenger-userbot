"""Доставка: сверка неоднозначной отправки, повторы, штрафы, шифрование."""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncIterator, Awaitable, Callable

import pytest

from maxub.config import Settings
from maxub.core.crypto import SecretBox, SecretError
from maxub.core.models import OutboxState, Session, utcnow
from maxub.core.review import discarded_event
from maxub.core.service import ServiceError, ServiceNotFound, UserbotService
from maxub.core.storage import Storage
from maxub.transport.base import (
    ReconcileOutcome,
    ReconcileResult,
    TransportNotApplied,
    TransportOutcomeUnknown,
    TransportRateLimited,
)
from maxub.transport.stub import StubTransport
from tests.conftest import build_service, login_by_phone, wait_for


@pytest.fixture
async def service(settings: Settings) -> AsyncIterator[UserbotService]:
    svc = build_service(settings)
    await svc.start()
    try:
        yield svc
    finally:
        await svc.stop()


async def _force_sending(service: UserbotService, item_id: int) -> None:
    """Возвращает запись в состояние «отправляется» — как при падении процесса."""
    await service._storage.db.execute(
        "UPDATE outbox SET state = ? WHERE id = ?", (OutboxState.SENDING.value, item_id)
    )
    await service._storage.db.commit()


def _awaits_state(
    service: UserbotService, item_id: int, state: OutboxState
) -> Callable[[], Awaitable[bool]]:
    """Условие для `wait_for`: запись дошла до нужного состояния."""

    async def condition() -> bool:
        stored = await service._storage.get_outbox(item_id)
        return stored is not None and stored.state is state

    return condition


async def _prepared_account(settings: Settings) -> int:
    """Оставляет в БД аккаунт с рабочей сессией и закрывает демон.

    Нужен, чтобы следующий запуск поднял аккаунт сам — как после падения.
    """
    service = build_service(settings)
    await service.start()
    try:
        return await login_by_phone(service)
    finally:
        await service.stop()


async def test_reconcile_marks_sent_when_server_has_message(service: UserbotService) -> None:
    """Если сообщение всё-таки дошло, повторно его не отправляем."""
    account_id = await login_by_phone(service)
    item, _ = await service.enqueue_message(account_id, "chat-1", "дошло")

    async def sent() -> bool:
        stored = await service._storage.get_outbox(item.id)
        return stored is not None and stored.state is OutboxState.SENT

    await wait_for(sent)
    await _force_sending(service, item.id)

    await service._worker.reconcile_stale()

    stored = await service._storage.get_outbox(item.id)
    assert stored is not None
    assert stored.state is OutboxState.SENT
    assert stored.remote_message_id is not None


async def test_reconcile_requeues_when_server_has_nothing(service: UserbotService) -> None:
    """Если сообщения на сервере нет — повтор безопасен."""
    account_id = await login_by_phone(service)
    transport = service._connections.get(account_id)
    assert isinstance(transport, StubTransport)
    transport.fail_sends = 100
    transport.fail_with = TransportNotApplied("канал занят")

    item, _ = await service.enqueue_message(account_id, "chat-1", "не дошло")
    await _force_sending(service, item.id)
    await service._worker.reconcile_stale()

    stored = await service._storage.get_outbox(item.id)
    assert stored is not None
    assert stored.state is OutboxState.QUEUED


async def test_claimed_batch_is_retried_not_failed(settings: Settings) -> None:
    """Пачка, захваченная перед падением процесса, повторяется, а не теряется.

    Транспорт после перезапуска намеренно ничего не может сверить: так видно,
    что записи, до которых очередь не дошла, разбираются вообще без сверки, а
    не уходят человеку вместе с той единственной, что успела в сеть.
    """
    account_id = await _prepared_account(settings)

    storage = Storage(settings.db_path, SecretBox(settings.resolve_secret_key()))
    await storage.open()
    ids = []
    for index in range(3):
        item, _ = await storage.enqueue(
            account_id, "chat-2", f"пачка {index}", f"ключ-{index}", 60.0
        )
        ids.append(item.id)
    claimed = await storage.claim_queued()
    assert [c.state for c in claimed] == [OutboxState.CLAIMED] * 3
    # Первая успела уйти в сеть, до остальных дело не дошло — и процесс упал.
    await storage.mark_sending(ids[0])
    await storage.close()

    after_crash = StubTransport()
    after_crash.reconcile_inconclusive = True
    service = UserbotService(
        settings,
        Storage(settings.db_path, SecretBox(settings.resolve_secret_key())),
        lambda: after_crash,
    )
    await service.start()
    try:
        await wait_for(_awaits_state(service, ids[0], OutboxState.FAILED))
        for item_id in ids[1:]:
            await wait_for(_awaits_state(service, item_id, OutboxState.SENT))
            stored = await service._storage.get_outbox(item_id)
            assert stored is not None
            assert stored.error is None
    finally:
        await service.stop()


async def test_repeated_crashes_exhaust_attempts(settings: Settings) -> None:
    """Сообщение, которое роняет процесс, не забирают в работу бесконечно.

    Захват засчитывается попыткой, поэтому цикл «захватили — упали» упирается в
    тот же лимит, что и обычные ошибки отправки.
    """
    settings.max_send_attempts = 2
    account_id = await _prepared_account(settings)

    storage = Storage(settings.db_path, SecretBox(settings.resolve_secret_key()))
    await storage.open()
    item, _ = await storage.enqueue(account_id, "chat-4", "роняет демон", "ключ-падения", 60.0)
    for _ in range(settings.max_send_attempts + 1):
        assert await storage.claim_queued()
        # Процесс упал сразу после захвата, разбор на старте вернул запись.
        assert await storage.release_all_claimed() == 1
    await storage.close()

    service = build_service(settings)
    await service.start()
    try:
        await wait_for(_awaits_state(service, item.id, OutboxState.FAILED))
        stored = await service._storage.get_outbox(item.id)
        assert stored is not None
        assert "исчерпан лимит попыток" in (stored.error or "")
        transport = service._connections.get(account_id)
        assert isinstance(transport, StubTransport)
        assert await transport.fetch_history("chat-4", 10) == []
    finally:
        await service.stop()


async def test_unknown_outcome_is_confirmed_by_reconcile(service: UserbotService) -> None:
    """Дошедшее сообщение без подтверждения не объявляется неудачей.

    Иначе человек увидел бы отказ и отправил бы то же самое ещё раз — уже
    вторым сообщением в чате.
    """
    account_id = await login_by_phone(service)
    transport = service._connections.get(account_id)
    assert isinstance(transport, StubTransport)
    transport.fail_sends = 1
    transport.fail_with = TransportOutcomeUnknown("ответ сервера не дошёл")

    item, _ = await service.enqueue_message(account_id, "chat-8", "дошло вслепую")
    await wait_for(_awaits_state(service, item.id, OutboxState.SENT))

    stored = await service._storage.get_outbox(item.id)
    assert stored is not None
    assert stored.remote_message_id is not None
    assert len(await transport.fetch_history("chat-8", 10)) == 1


async def test_inconclusive_reconcile_is_left_to_human(service: UserbotService) -> None:
    """Если исход выяснить не удалось, запись отдаётся человеку без повтора."""
    account_id = await login_by_phone(service)
    transport = service._connections.get(account_id)
    assert isinstance(transport, StubTransport)
    transport.fail_sends = 1
    transport.fail_with = TransportOutcomeUnknown("обрыв связи")
    transport.reconcile_inconclusive = True

    item, _ = await service.enqueue_message(account_id, "chat-7", "неизвестно")
    await wait_for(_awaits_state(service, item.id, OutboxState.FAILED))

    stored = await service._storage.get_outbox(item.id)
    assert stored is not None
    assert "исход отправки неизвестен" in (stored.error or "")
    # Повтора не было: сообщение, которое всё-таки дошло, осталось одно.
    assert len(await transport.fetch_history("chat-7", 10)) == 1


async def test_not_found_reconcile_delivers_exactly_once(settings: Settings) -> None:
    """Доказанное «не дошло» повторяется, и получатель видит одно сообщение."""
    # Длинная пауза перед повтором держит запись под контролем теста: воркер не
    # подхватит её в середине проверки.
    settings.retry_base_seconds = 30.0
    settings.retry_max_seconds = 30.0
    shared = StubTransport()
    service = UserbotService(
        settings,
        Storage(settings.db_path, SecretBox(settings.resolve_secret_key())),
        lambda: shared,
    )
    await service.start()
    try:
        account_id = await login_by_phone(service)
        # Отказ «точно не выполнено» не оставляет следа на сервере — именно то,
        # что потом должна доказать сверка.
        shared.fail_sends = 1
        shared.fail_with = TransportNotApplied("канал занят")
        item, _ = await service.enqueue_message(account_id, "chat-3", "ровно раз")

        async def parked() -> bool:
            stored = await service._storage.get_outbox(item.id)
            return stored is not None and stored.next_attempt_at is not None

        await wait_for(parked)
        # Следующая попытка как будто оборвалась вместе с процессом.
        await _force_sending(service, item.id)
        await service._worker.reconcile_stale()

        await wait_for(_awaits_state(service, item.id, OutboxState.SENT))
        assert [m.text for m in await shared.fetch_history("chat-3", 10)] == ["ровно раз"]
    finally:
        await service.stop()


async def _left_to_human(service: UserbotService, chat_id: str, text: str) -> int:
    """Доводит сообщение до отказа с неизвестным исходом и возвращает его id.

    Ровно тот случай, ради которого существует ручной разбор: сообщение ушло в
    сеть, сверка ничего не доказала, и решение осталось за человеком.
    """
    account_id = await login_by_phone(service)
    transport = service._connections.get(account_id)
    assert isinstance(transport, StubTransport)
    transport.fail_sends = 1
    transport.fail_with = TransportOutcomeUnknown("обрыв связи")
    transport.reconcile_inconclusive = True

    item, _ = await service.enqueue_message(account_id, chat_id, text)
    await wait_for(_awaits_state(service, item.id, OutboxState.FAILED))
    return item.id


async def test_stuck_items_are_listed_with_reason(service: UserbotService) -> None:
    """Список отказавших даёт всё, что нужно для решения по конкретной записи."""
    item_id = await _left_to_human(service, "chat-review", "разобрать вручную")

    listed = await service.list_stuck_messages(limit=50)
    row = next(r for r in listed if r["id"] == item_id)
    assert row["state"] == OutboxState.FAILED.value
    assert row["chat_id"] == "chat-review"
    assert "исход отправки неизвестен" in str(row["error"])
    assert int(str(row["attempts"])) >= 1
    assert row["created_at"]

    # Отправленное человека не касается: в списке его быть не должно, иначе
    # разбирать пришлось бы всю очередь целиком.
    delivered, _ = await service.enqueue_message(1, "chat-review", "дошло само")
    await wait_for(_awaits_state(service, delivered.id, OutboxState.SENT))
    assert all(r["id"] != delivered.id for r in await service.list_stuck_messages(limit=50))


async def test_manual_retry_delivers_failed_item_once(settings: Settings) -> None:
    """Повтор отказавшей записи действительно уходит — и ровно один раз.

    Лимит попыток исчерпан отказом, поэтому без сброса счётчика воркер закрыл бы
    запись, не дойдя до транспорта, и повтор оказался бы бумажным.
    """
    settings.max_send_attempts = 1
    service = build_service(settings)
    await service.start()
    try:
        account_id = await login_by_phone(service)
        transport = service._connections.get(account_id)
        assert isinstance(transport, StubTransport)
        # «Точно не выполнено»: на сервере следа не остаётся, и сверка потом
        # честно отвечает «сообщения нет».
        transport.fail_sends = 1
        transport.fail_with = TransportNotApplied("канал занят")

        item, _ = await service.enqueue_message(account_id, "chat-manual", "повторить вручную")
        await wait_for(_awaits_state(service, item.id, OutboxState.FAILED))

        result = await service.retry_message(item.id)
        assert result["requeued"] is True
        assert result["check"] == "not_found"
        assert result["duplicate_risk"] is False

        await wait_for(_awaits_state(service, item.id, OutboxState.SENT))
        assert [m.text for m in await transport.fetch_history("chat-manual", 10)] == [
            "повторить вручную"
        ]
    finally:
        await service.stop()


async def test_manual_retry_does_not_duplicate_delivered_message(service: UserbotService) -> None:
    """Если сверка нашла сообщение на сервере, повтора не будет.

    Человек просит доставку, а не второй сетевой вызов: доказанная доставка
    закрывает запись, и получатель не видит дубль.
    """
    item_id = await _left_to_human(service, "chat-found", "дошло вслепую")
    transport = service._connections.get(1)
    assert isinstance(transport, StubTransport)
    # Связь восстановилась, и теперь сервер может ответить о судьбе сообщения.
    transport.reconcile_inconclusive = False

    result = await service.retry_message(item_id)

    assert result["requeued"] is False
    assert result["check"] == "found"
    assert result["duplicate_risk"] is False
    stored = await service._storage.get_outbox(item_id)
    assert stored is not None
    assert stored.state is OutboxState.SENT
    assert len(await transport.fetch_history("chat-found", 10)) == 1


async def test_manual_retry_reports_duplicate_risk(service: UserbotService) -> None:
    """Повтор без доказательств разрешён, но риск дубля назван прямо."""
    item_id = await _left_to_human(service, "chat-risk", "под ответственность")

    result = await service.retry_message(item_id)

    assert result["requeued"] is True
    assert result["check"] == "inconclusive"
    assert result["duplicate_risk"] is True
    assert "дубль" in str(result["detail"])


async def test_manual_retry_refuses_live_item(settings: Settings) -> None:
    """Запись, которой распоряжается воркер, у него не отбирают.

    Долгая пауза перед повтором держит запись в очереди: она жива, просто ждёт
    своего срока, и ручное вмешательство сюда не допускается.
    """
    settings.retry_base_seconds = 30.0
    settings.retry_max_seconds = 30.0
    service = build_service(settings)
    await service.start()
    try:
        account_id = await login_by_phone(service)
        transport = service._connections.get(account_id)
        assert isinstance(transport, StubTransport)
        transport.fail_sends = 100
        transport.fail_with = TransportNotApplied("канал занят")

        item, _ = await service.enqueue_message(account_id, "chat-live", "живая запись")

        async def parked() -> bool:
            stored = await service._storage.get_outbox(item.id)
            return stored is not None and stored.next_attempt_at is not None

        await wait_for(parked)

        with pytest.raises(ServiceError) as excinfo:
            await service.retry_message(item.id)
        # Именно конфликт состояния, а не «не найдено»: код выхода у них разный.
        assert not isinstance(excinfo.value, ServiceNotFound)

        stored = await service._storage.get_outbox(item.id)
        assert stored is not None
        assert stored.state is OutboxState.QUEUED
        assert stored.attempts == 1
        assert stored.next_attempt_at is not None
    finally:
        await service.stop()


async def test_concurrent_manual_retries_do_not_duplicate(service: UserbotService) -> None:
    """Два одновременных повтора одной записи не отправляют её дважды.

    Худший случай — когда первому запросу сверка отвечает «сообщение на
    сервере», а второму (пока первый ещё ждёт ответа) выяснить не удаётся: без
    порядка разбора второй вернул бы запись в очередь и получатель увидел бы
    дубль.
    """
    item_id = await _left_to_human(service, "chat-race", "гонка разборов")
    transport = service._connections.get(1)
    assert isinstance(transport, StubTransport)
    transport.reconcile_inconclusive = False
    honest = transport.reconcile_send
    calls = 0

    async def slow_then_inconclusive(chat_id: str, client_token: str) -> ReconcileResult:
        nonlocal calls
        calls += 1
        if calls == 1:
            await asyncio.sleep(0.2)
            return await honest(chat_id, client_token)
        return ReconcileResult(outcome=ReconcileOutcome.INCONCLUSIVE, detail="имитация гонки")

    transport.reconcile_send = slow_then_inconclusive  # type: ignore[method-assign]

    outcomes = await asyncio.gather(
        service.retry_message(item_id),
        service.retry_message(item_id),
        return_exceptions=True,
    )

    decided = [o for o in outcomes if isinstance(o, dict)]
    refused = [o for o in outcomes if isinstance(o, ServiceError)]
    assert len(decided) == 1
    assert len(refused) == 1
    assert decided[0]["check"] == "found"
    assert len(await transport.fetch_history("chat-race", 10)) == 1


async def test_manual_retry_of_missing_item_is_not_found(service: UserbotService) -> None:
    """Несуществующая запись — это «не найдено», а не отказ общего вида."""
    with pytest.raises(ServiceNotFound):
        await service.retry_message(424242)


async def _refused_without_trace(service: UserbotService, chat_id: str, text: str) -> int:
    """Доводит сообщение до отказа, не оставив следа на сервере.

    Отличается от [_left_to_human] намеренно: там сообщение всё-таки ушло, и по
    истории чата не видно, отправляли ли его ещё раз. Здесь история пуста, а
    транспорт после отказа исправен — значит любая непрошеная отправка сразу
    видна.
    """
    account_id = await login_by_phone(service)
    transport = service._connections.get(account_id)
    assert isinstance(transport, StubTransport)
    transport.fail_sends = 100
    transport.fail_with = TransportNotApplied("канал занят")

    item, _ = await service.enqueue_message(account_id, chat_id, text)
    await wait_for(_awaits_state(service, item.id, OutboxState.FAILED))
    transport.fail_sends = 0
    return item.id


async def test_discarded_item_is_never_sent_and_leaves_a_trail(service: UserbotService) -> None:
    """Запись, от которой отказались, не уезжает и не зовёт человека второй раз.

    Проверяется вместе: воркер её больше не берёт, в списке ждущих разбора её
    нет, а причины — и отказа отправки, и решения человека — сохранены обе.
    """
    item_id = await _refused_without_trace(service, "chat-discard", "уже не нужно")

    stored = await service.discard_message(item_id, "чат закрыт, отправлять некуда")

    assert stored["state"] == OutboxState.DISCARDED.value
    assert stored["discard_reason"] == "чат закрыт, отправлять некуда"
    assert stored["discarded_at"]
    # Ошибка отправки цела: она объясняет, почему запись вообще попала к
    # человеку, и решение не должно её затирать.
    assert "канал занят" in str(stored["error"])

    assert all(r["id"] != item_id for r in await service.list_stuck_messages(limit=50))
    # Транспорт исправен, и несколько циклов воркера прошли: если бы запись
    # осталась живой, сообщение появилось бы в чате.
    await asyncio.sleep(0.3)
    transport = service._connections.get(1)
    assert isinstance(transport, StubTransport)
    assert await transport.fetch_history("chat-discard", 10) == []
    assert (await service._storage.get_outbox(item_id)) is not None


async def test_discard_refuses_live_item(settings: Settings) -> None:
    """Живую запись у демона не отбирают даже отказом.

    Отмена на ходу опаснее отказа в повторе: отправка могла уже уйти в сеть, а
    запись при этом объявили бы «решили не отправлять».
    """
    settings.retry_base_seconds = 30.0
    settings.retry_max_seconds = 30.0
    service = build_service(settings)
    await service.start()
    try:
        account_id = await login_by_phone(service)
        transport = service._connections.get(account_id)
        assert isinstance(transport, StubTransport)
        transport.fail_sends = 100
        transport.fail_with = TransportNotApplied("канал занят")

        item, _ = await service.enqueue_message(account_id, "chat-live-discard", "живая запись")

        async def parked() -> bool:
            stored = await service._storage.get_outbox(item.id)
            return stored is not None and stored.next_attempt_at is not None

        await wait_for(parked)

        with pytest.raises(ServiceError) as excinfo:
            await service.discard_message(item.id, "передумал")
        # Конфликт состояния, а не «не найдено»: коды выхода у них разные.
        assert not isinstance(excinfo.value, ServiceNotFound)

        stored = await service._storage.get_outbox(item.id)
        assert stored is not None
        assert stored.state is OutboxState.QUEUED
        assert stored.discard_reason is None
    finally:
        await service.stop()


async def test_discard_of_missing_item_is_not_found(service: UserbotService) -> None:
    """Отказ от несуществующей записи — «не найдено», а не конфликт."""
    with pytest.raises(ServiceNotFound):
        await service.discard_message(424242, "нечего отменять")


async def test_discarded_item_cannot_be_retried(service: UserbotService) -> None:
    """Решение окончательно: повторить запись после отказа нельзя."""
    item_id = await _refused_without_trace(service, "chat-final", "решено не слать")
    await service.discard_message(item_id, "дублирует сообщение из другого канала")

    with pytest.raises(ServiceError) as excinfo:
        await service.retry_message(item_id)
    assert not isinstance(excinfo.value, ServiceNotFound)


async def test_discarded_state_is_counted_in_status(service: UserbotService) -> None:
    """Новое состояние обязано быть видно в статусе, а не молча выпасть."""
    before = (await service.status())["outbox"]
    assert isinstance(before, dict)
    # Состояние показывается нулём ещё до первого отказа: иначе по статусу
    # нельзя отличить «таких записей нет» от «их не считают».
    assert before[OutboxState.DISCARDED.value] == 0

    item_id = await _refused_without_trace(service, "chat-stats", "в статистику")
    await service.discard_message(item_id, "неактуально")

    after = (await service.status())["outbox"]
    assert isinstance(after, dict)
    assert after[OutboxState.DISCARDED.value] == 1
    assert after[OutboxState.FAILED.value] == 0


async def test_discard_without_reason_is_a_programming_error(service: UserbotService) -> None:
    """Правило «причина обязательна» принадлежит ядру, а не схеме запроса.

    Проверка на границе HTTP отсекает пустую строку раньше, но плагин ходит в
    сервис напрямую — и записать окончательное решение без объяснения не должен
    и он.
    """
    item_id = await _refused_without_trace(service, "chat-noreason", "без объяснения")

    for empty in ("", "   "):
        with pytest.raises(ValueError):
            await service.discard_message(item_id, empty)

    stored = await service._storage.get_outbox(item_id)
    assert stored is not None
    assert stored.state is OutboxState.FAILED


async def test_known_discard_event_is_not_shown_twice(service: UserbotService) -> None:
    """Отказ, который журнал уже видел, подписчику второй раз не показывают.

    Случай возможен только при вмешательстве в базу мимо демона, но именно
    тогда и важно, чтобы поток событий не разошёлся с журналом.
    """
    item_id = await _refused_without_trace(service, "chat-twice", "повторное событие")
    stored = await service._storage.get_outbox(item_id)
    assert stored is not None
    await service._storage.record_event(discarded_event(stored, "занятый ключ"))

    queue = service.subscribe()
    try:
        result = await service.discard_message(item_id, "решение принято")
    finally:
        service.unsubscribe(queue)

    assert result["state"] == OutboxState.DISCARDED.value
    assert queue.empty()
    events = await service.recent_events(limit=50, after_id=0)
    assert len([e for e in events if e["kind"] == "message.discarded"]) == 1


async def test_discard_is_recorded_in_events(service: UserbotService) -> None:
    """Подписчик узнаёт и такой исход: иначе отправка для него не завершится."""
    item_id = await _refused_without_trace(service, "chat-event", "в журнал")
    await service.discard_message(item_id, "адресат уже ответил")

    events = await service.recent_events(limit=50, after_id=0)
    discarded = [e for e in events if e["kind"] == "message.discarded"]
    assert len(discarded) == 1
    payload = discarded[0]["payload"]
    assert isinstance(payload, dict)
    assert payload["outbox_id"] == item_id
    assert payload["reason"] == "адресат уже ответил"


async def test_retry_is_scheduled_not_immediate(service: UserbotService) -> None:
    """Повтор откладывается и переживает перезапуск: срок лежит в БД."""
    account_id = await login_by_phone(service)
    transport = service._connections.get(account_id)
    assert isinstance(transport, StubTransport)
    transport.fail_sends = 1
    transport.fail_with = TransportNotApplied("временно недоступно")

    item, _ = await service.enqueue_message(account_id, "chat-1", "с повтором")

    async def scheduled() -> bool:
        stored = await service._storage.get_outbox(item.id)
        return stored is not None and stored.next_attempt_at is not None

    await wait_for(scheduled)

    async def eventually_sent() -> bool:
        stored = await service._storage.get_outbox(item.id)
        return stored is not None and stored.state is OutboxState.SENT

    await wait_for(eventually_sent)
    stored = await service._storage.get_outbox(item.id)
    assert stored is not None
    assert stored.attempts >= 2


async def test_attempts_are_capped(settings: Settings) -> None:
    """Бесконечных повторов не бывает: лимит попыток заканчивается отказом."""
    settings.max_send_attempts = 2
    service = build_service(settings)
    await service.start()
    try:
        account_id = await login_by_phone(service)
        transport = service._connections.get(account_id)
        assert isinstance(transport, StubTransport)
        transport.fail_sends = 100
        transport.fail_with = TransportNotApplied("постоянно недоступно")

        item, _ = await service.enqueue_message(account_id, "chat-1", "обречено")

        async def failed() -> bool:
            stored = await service._storage.get_outbox(item.id)
            return stored is not None and stored.state is OutboxState.FAILED

        await wait_for(failed)
        stored = await service._storage.get_outbox(item.id)
        assert stored is not None
        assert "исчерпан лимит попыток" in (stored.error or "")
    finally:
        await service.stop()


async def test_rate_limit_penalty_is_persisted(service: UserbotService) -> None:
    """Штраф от сервера должен пережить перезапуск демона."""
    account_id = await login_by_phone(service)
    transport = service._connections.get(account_id)
    assert isinstance(transport, StubTransport)
    transport.fail_sends = 1
    transport.fail_with = TransportRateLimited("слишком часто", retry_after=30.0)

    await service.enqueue_message(account_id, "chat-1", "по лимиту")

    async def penalized() -> bool:
        return bool(await service._storage.load_penalties())

    await wait_for(penalized)
    penalties = await service._storage.load_penalties()
    assert penalties[0][0] == account_id
    assert penalties[0][1] == "send_text"


async def test_backfill_recovers_missed_messages(settings: Settings) -> None:
    """Пропущенное за время простоя добирается по курсору при подключении.

    Транспорт здесь один на все подключения — так воспроизводится сервер,
    который помнит сообщения, пока демон был отключён.
    """
    shared = StubTransport()
    service = UserbotService(
        settings,
        Storage(settings.db_path, SecretBox(settings.resolve_secret_key())),
        lambda: shared,
    )
    await service.start()
    try:
        account_id = await login_by_phone(service)
        # Сообщение приходит мимо потока событий — как будто демон был выключен.
        shared.add_missed("chat-9", "пропущенное")

        session = Session.model_validate(await service._storage.load_session(account_id))
        await service._connections.disconnect(account_id)
        await service._connections.connect(account_id, session)

        events = await service.recent_events(limit=50, after_id=0)
        payloads = [e["payload"] for e in events if e["kind"] == "message.received"]
        texts = [p.get("text") for p in payloads if isinstance(p, dict)]
        assert "пропущенное" in texts
    finally:
        await service.stop()


async def test_backfill_does_not_duplicate_on_second_connect(settings: Settings) -> None:
    """Повторное подключение не должно заново пересылать уже виденное."""
    shared = StubTransport()
    service = UserbotService(
        settings,
        Storage(settings.db_path, SecretBox(settings.resolve_secret_key())),
        lambda: shared,
    )
    await service.start()
    try:
        account_id = await login_by_phone(service)
        shared.add_missed("chat-9", "однажды")
        session = Session.model_validate(await service._storage.load_session(account_id))

        for _ in range(2):
            await service._connections.disconnect(account_id)
            await service._connections.connect(account_id, session)

        events = await service.recent_events(limit=50, after_id=0)
        received = [e for e in events if e["kind"] == "message.received"]
        assert len(received) == 1
    finally:
        await service.stop()


async def test_session_is_encrypted_at_rest(settings: Settings) -> None:
    """Сырой файл БД не должен содержать токен сессии открытым текстом."""
    service = build_service(settings)
    await service.start()
    try:
        account_id = await login_by_phone(service)
        payload = await service._storage.load_session(account_id)
        assert payload is not None
        token = str(payload["token"])
    finally:
        await service.stop()

    raw = sqlite3.connect(settings.db_path)
    stored = raw.execute("SELECT payload FROM sessions").fetchone()[0]
    raw.close()
    assert token not in stored
    assert "stub-" not in stored


async def test_wrong_key_cannot_read_sessions(settings: Settings) -> None:
    """Копия БД без ключа бесполезна."""
    service = build_service(settings)
    await service.start()
    try:
        account_id = await login_by_phone(service)
    finally:
        await service.stop()

    from maxub.core.crypto import generate_key

    foreign = Storage(settings.db_path, SecretBox(generate_key()))
    await foreign.open()
    try:
        with pytest.raises(SecretError):
            await foreign.load_session(account_id)
    finally:
        await foreign.close()


async def test_long_penalty_does_not_stall_other_accounts(settings: Settings) -> None:
    """Штраф по одному аккаунту не останавливает отправку у остальных.

    Воркер очереди один на весь демон. Пока он высиживал штраф прямо в цикле,
    получасовой отказ по одному аккаунту означал получасовой простой у всех —
    ровно то, ради чего мультиаккаунт и заводился.
    """
    service = build_service(settings)
    await service.start()
    try:
        slow = await login_by_phone(service, "+79990000201")
        fast = await login_by_phone(service, "+79990000202")
        transport = service._connections.get(slow)
        assert isinstance(transport, StubTransport)
        transport.fail_sends = 1
        transport.fail_with = TransportRateLimited("слишком часто", retry_after=1800.0)

        stuck, _ = await service.enqueue_message(slow, "chat-slow", "первое")
        await wait_for(lambda: _state_is(service, stuck.id, OutboxState.QUEUED))
        blocked, _ = await service.enqueue_message(slow, "chat-slow", "второе")
        delivered, _ = await service.enqueue_message(fast, "chat-fast", "чужое")

        await wait_for(lambda: _state_is(service, delivered.id, OutboxState.SENT))
        assert await _state_is(service, blocked.id, OutboxState.QUEUED)
    finally:
        await service.stop()


async def test_deferred_message_keeps_its_attempts(settings: Settings) -> None:
    """Откладывание по лимиту не тратит попытки.

    Попытку засчитывает захват записи, а в сеть отложенная запись не уходила.
    Без отката счётчика сообщение под долгим штрафом исчерпало бы лимит попыток,
    ни разу не побывав у сервера, и закрылось бы как отказавшее.
    """
    service = build_service(settings)
    await service.start()
    try:
        account_id = await login_by_phone(service, "+79990000203")
        transport = service._connections.get(account_id)
        assert isinstance(transport, StubTransport)
        transport.fail_sends = 1
        transport.fail_with = TransportRateLimited("слишком часто", retry_after=1800.0)

        first, _ = await service.enqueue_message(account_id, "chat-1", "первое")
        await wait_for(lambda: _state_is(service, first.id, OutboxState.QUEUED))
        item, _ = await service.enqueue_message(account_id, "chat-1", "второе")

        # Дать воркеру несколько кругов: каждый круг — один захват записи.
        await asyncio.sleep(2.0)
        stored = await service._storage.get_outbox(item.id)
        assert stored is not None
        assert stored.state is OutboxState.QUEUED
        assert stored.attempts == 0
        assert stored.next_attempt_at is not None
    finally:
        await service.stop()


async def test_rate_limit_without_retry_after_still_penalizes(settings: Settings) -> None:
    """Отказ по лимиту без указания срока штрафуется запасным значением.

    Иначе штрафа нет вовсе, остаётся общий backoff в несколько секунд — и демон
    возвращается ровно туда, откуда его только что прогнали.
    """
    service = build_service(settings.model_copy(update={"rate_limit_fallback_seconds": 900.0}))
    await service.start()
    try:
        account_id = await login_by_phone(service, "+79990000204")
        transport = service._connections.get(account_id)
        assert isinstance(transport, StubTransport)
        transport.fail_sends = 1
        transport.fail_with = TransportRateLimited("слишком часто", retry_after=None)

        await service.enqueue_message(account_id, "chat-1", "без срока")

        await wait_for(lambda: _has_penalty(service))
        penalties = await service._storage.load_penalties()
        assert (penalties[0][2] - utcnow()).total_seconds() > 600
    finally:
        await service.stop()


async def _state_is(service: UserbotService, item_id: int, state: OutboxState) -> bool:
    stored = await service._storage.get_outbox(item_id)
    return stored is not None and stored.state is state


async def _has_penalty(service: UserbotService) -> bool:
    return bool(await service._storage.load_penalties())
