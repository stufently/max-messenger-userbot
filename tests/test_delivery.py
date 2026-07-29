"""Доставка: сверка неоднозначной отправки, повторы, штрафы, шифрование."""

from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator, Awaitable, Callable

import pytest

from maxub.config import Settings
from maxub.core.crypto import SecretBox, SecretError
from maxub.core.models import OutboxState, Session
from maxub.core.service import UserbotService
from maxub.core.storage import Storage
from maxub.transport.base import (
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
