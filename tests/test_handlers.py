"""Тесты точки расширения: контракт, реестр и разбор журнала.

Обработчик здесь поддельный — настоящих в поставке нет. Проверяется сам договор:
что приходит, что не приходит, что происходит с курсором при ошибке и что
отправка обработчика остаётся идемпотентной после перезапуска.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import timedelta

import pytest

from maxub.config import Settings
from maxub.core.crypto import SecretBox
from maxub.core.handlers import HandlerDispatcher, HandlerRegistry, HandlerRegistryError
from maxub.core.handlers.contract import HandlerContext
from maxub.core.housekeeping import Housekeeper
from maxub.core.models import Event, utcnow
from maxub.core.storage import Storage
from maxub.transport.base import Capabilities
from tests.conftest import wait_for


class Recorder:
    """Обработчик, который запоминает всё, что ему дали."""

    def __init__(
        self,
        name: str = "recorder",
        kinds: frozenset[str] = frozenset(),
        requires: frozenset[str] = frozenset(),
        fail_times: int = 0,
        sends: int = 0,
    ) -> None:
        self.name = name
        self.kinds = kinds
        self.requires = requires
        self.seen: list[tuple[int, str]] = []
        self._fail_times = fail_times
        self._sends = sends

    async def handle(self, event: Event, context: HandlerContext) -> None:
        self.seen.append((context.event_id, event.kind))
        if self._fail_times > 0:
            self._fail_times -= 1
            raise RuntimeError("не сегодня")
        for index in range(self._sends):
            await context.send_text("чат", f"ответ {index}")


class FakeActions:
    """Очередь отправки и возможности транспорта в виде списков."""

    def __init__(self, capabilities: Capabilities | None = None) -> None:
        self.sent: list[tuple[int, str, str, str]] = []
        self._capabilities = capabilities

    async def enqueue(self, account_id: int, chat_id: str, text: str, nonce: str) -> int:
        self.sent.append((account_id, chat_id, text, nonce))
        return len(self.sent)

    def transport_capabilities(self, account_id: int) -> Capabilities | None:
        return self._capabilities


def event(kind: str = "message.received", key: str = "", account_id: int | None = 1) -> Event:
    return Event(
        account_id=account_id,
        kind=kind,
        dedup_key=key or f"{kind}:{utcnow().isoformat()}",
    )


async def storage_of(settings: Settings) -> Storage:
    storage = Storage(settings.db_path, SecretBox(settings.resolve_secret_key()))
    await storage.open()
    return storage


@contextlib.asynccontextmanager
async def running(dispatcher: HandlerDispatcher) -> AsyncIterator[None]:
    """Крутит диспетчер на время блока и обязательно останавливает его."""
    task = asyncio.ensure_future(dispatcher.run())
    try:
        yield
    finally:
        dispatcher.stop()
        await task


def seen(handler: Recorder, count: int = 1) -> Callable[[], Awaitable[bool]]:
    async def condition() -> bool:
        return len(handler.seen) >= count

    return condition


def sent(actions: FakeActions, count: int = 1) -> Callable[[], Awaitable[bool]]:
    async def condition() -> bool:
        return len(actions.sent) >= count

    return condition


def recorded(storage: Storage, kind: str) -> Callable[[], Awaitable[bool]]:
    async def condition() -> bool:
        return any(item.kind == kind for _, item in await storage.list_events(limit=100))

    return condition


def build(
    storage: Storage, handler: Recorder, actions: FakeActions, **extra: object
) -> HandlerDispatcher:
    return HandlerDispatcher(
        journal=storage,
        registry=HandlerRegistry([handler]),
        actions=actions,
        publish=lambda _: None,
        idle_seconds=0.01,
        **extra,  # type: ignore[arg-type]
    )


# --- реестр ------------------------------------------------------------------


def test_duplicate_name_is_refused() -> None:
    with pytest.raises(HandlerRegistryError):
        HandlerRegistry([Recorder(), Recorder()])


def test_unknown_capability_is_refused() -> None:
    """Опечатка в требовании — отказ при сборке, а не тихий простой в работе."""
    with pytest.raises(HandlerRegistryError) as refused:
        HandlerRegistry([Recorder(requires=frozenset({"send_txt"}))])
    assert "send_txt" in str(refused.value)


def test_empty_name_is_refused() -> None:
    with pytest.raises(HandlerRegistryError):
        HandlerRegistry([Recorder(name="")])


# --- разбор журнала ----------------------------------------------------------


async def test_new_handler_starts_from_the_end(settings: Settings) -> None:
    """Включённый сегодня обработчик не разбирает трёхмесячную историю."""
    storage = await storage_of(settings)
    try:
        await storage.record_event(event(key="старое"))
        handler, actions = Recorder(), FakeActions()
        dispatcher = build(storage, handler, actions)

        await dispatcher.prepare()
        await storage.record_event(event(key="новое"))
        async with running(dispatcher):
            await wait_for(seen(handler))

        assert [kind for _, kind in handler.seen] == ["message.received"]
        assert len(handler.seen) == 1
    finally:
        await storage.close()


async def test_only_declared_kinds_arrive(settings: Settings) -> None:
    storage = await storage_of(settings)
    try:
        handler = Recorder(kinds=frozenset({"account.ready"}))
        dispatcher = build(storage, handler, FakeActions())
        await dispatcher.prepare()

        await storage.record_event(event(kind="message.received", key="чужое"))
        await storage.record_event(event(kind="account.ready", key="своё"))
        async with running(dispatcher):
            await wait_for(seen(handler))

        assert [kind for _, kind in handler.seen] == ["account.ready"]
    finally:
        await storage.close()


async def test_cursor_survives_restart(settings: Settings) -> None:
    """Разобранное второй раз не приходит, неразобранное — приходит."""
    storage = await storage_of(settings)
    try:
        first = Recorder()
        dispatcher = build(storage, first, FakeActions())
        await dispatcher.prepare()
        await storage.record_event(event(key="первое"))
        async with running(dispatcher):
            await wait_for(seen(first))

        second = Recorder()
        restarted = build(storage, second, FakeActions())
        await restarted.prepare()
        await storage.record_event(event(key="второе"))
        async with running(restarted):
            await wait_for(seen(second))

        assert len(second.seen) == 1
        assert second.seen[0][0] > first.seen[0][0]
    finally:
        await storage.close()


async def test_send_is_idempotent_across_repeats(settings: Settings) -> None:
    """Повтор события после падения не даёт второго сообщения."""
    storage = await storage_of(settings)
    try:
        handler, actions = Recorder(sends=2), FakeActions()
        dispatcher = build(storage, handler, actions)
        await dispatcher.prepare()
        await storage.record_event(event(key="повод"))
        async with running(dispatcher):
            await wait_for(sent(actions, 2))

        nonces = [nonce for *_, nonce in actions.sent]
        # Два вызова на одно событие — два разных ключа, а не один.
        assert len(set(nonces)) == 2
        # Ключ выводится из имени обработчика и номера события: тот же разбор
        # того же события даст те же ключи, и очередь опознает повтор.
        event_id = handler.seen[0][0]
        assert nonces == [f"handler:recorder:{event_id}:0", f"handler:recorder:{event_id}:1"]
    finally:
        await storage.close()


async def test_failing_handler_retries_then_gives_up(settings: Settings) -> None:
    """Сбойное событие повторяется, но не блокирует обработчика навсегда."""
    storage = await storage_of(settings)
    try:
        handler = Recorder(fail_times=99)
        dispatcher = build(storage, handler, FakeActions(), max_attempts=2)
        await dispatcher.prepare()
        await storage.record_event(event(key="проклятое"))
        await storage.record_event(event(key="обычное"))

        async with running(dispatcher):
            await wait_for(seen(handler, 3))

        # Первое событие пробовалось дважды, потом отложено — и обработчик
        # добрался до следующего.
        attempts = [event_id for event_id, _ in handler.seen]
        assert attempts[0] == attempts[1]
        assert attempts[2] > attempts[0]
        journal = await storage.list_events(limit=100)
        failures = [item for _, item in journal if item.kind == "handler.failed"]
        assert failures and failures[0].payload["attempts"] == 2
    finally:
        await storage.close()


async def test_missing_capability_blocks_the_event(settings: Settings) -> None:
    """Транспорт не умеет — обработчик не зовётся, но в журнале это видно."""
    storage = await storage_of(settings)
    try:
        handler = Recorder(requires=frozenset({"send_text"}))
        actions = FakeActions(Capabilities(send_text=False))
        dispatcher = build(storage, handler, actions)
        await dispatcher.prepare()
        await storage.record_event(event(key="повод"))

        async with running(dispatcher):
            await wait_for(recorded(storage, "handler.capability_missing"))

        assert handler.seen == []
    finally:
        await storage.close()


async def test_unknown_capability_is_not_a_refusal(settings: Settings) -> None:
    """Нет соединения — не «не умеет»: событие всё равно доходит."""
    storage = await storage_of(settings)
    try:
        handler = Recorder(requires=frozenset({"send_text"}))
        dispatcher = build(storage, handler, FakeActions(capabilities=None))
        await dispatcher.prepare()
        await storage.record_event(event(key="повод"))

        async with running(dispatcher):
            await wait_for(seen(handler))

        assert len(handler.seen) == 1
    finally:
        await storage.close()


async def test_service_events_are_not_delivered_back(settings: Settings) -> None:
    """Обработчик не получает собственных отказов — иначе они плодят себя сами."""
    storage = await storage_of(settings)
    try:
        handler = Recorder(fail_times=99)
        dispatcher = build(storage, handler, FakeActions(), max_attempts=1)
        await dispatcher.prepare()
        await storage.record_event(event(key="проклятое"))

        async with running(dispatcher):
            await wait_for(recorded(storage, "handler.failed"))
            # Отказ записан; если бы он доходил до обработчика, тот упал бы на
            # нём и записал следующий — и так без конца.
            await asyncio.sleep(0.2)

        journal = await storage.list_events(limit=100)
        assert len([item for _, item in journal if item.kind == "handler.failed"]) == 1
        assert len(handler.seen) == 1
    finally:
        await storage.close()


async def test_handler_cannot_choose_another_account(settings: Settings) -> None:
    """Отправка привязана к аккаунту события: выбрать другой нечем."""
    storage = await storage_of(settings)
    try:
        handler, actions = Recorder(sends=1), FakeActions()
        dispatcher = build(storage, handler, actions)
        await dispatcher.prepare()
        await storage.record_event(event(key="повод", account_id=7))
        async with running(dispatcher):
            await wait_for(sent(actions))

        assert [account_id for account_id, *_ in actions.sent] == [7]
    finally:
        await storage.close()


async def test_event_without_account_cannot_send(settings: Settings) -> None:
    """Событию без аккаунта отправлять не от кого — это отказ, а не тихий пропуск."""
    storage = await storage_of(settings)
    try:
        handler, actions = Recorder(sends=1), FakeActions()
        dispatcher = build(storage, handler, actions, max_attempts=1)
        await dispatcher.prepare()
        await storage.record_event(event(key="ничьё", account_id=None))

        async with running(dispatcher):
            await wait_for(recorded(storage, "handler.failed"))

        assert actions.sent == []
    finally:
        await storage.close()


async def test_event_without_account_is_skipped_when_capabilities_needed(
    settings: Settings,
) -> None:
    """Требующий возможностей обработчик не зовётся на событие без аккаунта."""
    storage = await storage_of(settings)
    try:
        handler = Recorder(requires=frozenset({"send_text"}))
        dispatcher = build(storage, handler, FakeActions(Capabilities(send_text=True)))
        await dispatcher.prepare()
        await storage.record_event(event(key="ничьё", account_id=None))

        async with running(dispatcher):
            await wait_for(recorded(storage, "handler.skipped"))

        assert handler.seen == []
    finally:
        await storage.close()


async def test_gap_is_recorded_when_the_tail_was_pruned(settings: Settings) -> None:
    """Съеденный уборкой хвост — это запись в журнале, а не молчание."""
    storage = await storage_of(settings)
    try:
        await storage.record_event(event(key="первое"))
        await storage.record_event(event(key="второе"))
        # Обработчик остановился на первом событии, а уборка съела оба.
        await storage.init_handler_cursor("recorder", 1)
        await storage.prune_events(utcnow() + timedelta(days=1))
        await storage.record_event(event(key="свежее"))

        dispatcher = build(storage, Recorder(), FakeActions())
        await dispatcher.prepare()

        journal = await storage.list_events(limit=100)
        gaps = [item for _, item in journal if item.kind == "handler.gap"]
        assert gaps and gaps[0].payload["handler"] == "recorder"
    finally:
        await storage.close()


async def test_context_account_cannot_be_reassigned(settings: Settings) -> None:
    """Аккаунт в контексте только читается: переписать его перед отправкой нельзя."""

    class Impostor(Recorder):
        async def handle(self, event: Event, context: HandlerContext) -> None:
            self.seen.append((context.event_id, event.kind))
            with pytest.raises(AttributeError):
                context.account_id = 8  # type: ignore[misc]
            await context.send_text("чат", "от кого положено")

    storage = await storage_of(settings)
    try:
        handler, actions = Impostor(), FakeActions()
        dispatcher = build(storage, handler, actions)
        await dispatcher.prepare()
        await storage.record_event(event(key="повод", account_id=3))

        async with running(dispatcher):
            await wait_for(sent(actions))

        assert [account_id for account_id, *_ in actions.sent] == [3]
    finally:
        await storage.close()


async def test_stale_advance_changes_nothing(settings: Settings) -> None:
    """Отставший экземпляр не отматывает курсор, не гасит попытки и не врёт в журнал."""
    storage = await storage_of(settings)
    try:
        await storage.init_handler_cursor("recorder", 0)
        await storage.advance_handler_cursor("recorder", 10)
        assert await storage.bump_handler_attempts("recorder", 10) == 1

        written = await storage.advance_handler_cursor(
            "recorder",
            4,
            Event(account_id=None, kind="handler.failed", dedup_key="ложный отказ"),
        )

        assert written is False
        assert await storage.load_handler_cursor("recorder") == 10
        assert await storage.load_handler_attempts("recorder") == 1
        journal = await storage.list_events(limit=100)
        assert [item.dedup_key for _, item in journal] == []
    finally:
        await storage.close()


async def test_attempts_belong_to_the_event(settings: Settings) -> None:
    """Попытка засчитывается только тому событию, с которого её взяли."""
    storage = await storage_of(settings)
    try:
        await storage.init_handler_cursor("recorder", 0)
        await storage.advance_handler_cursor("recorder", 10)

        # Позиция чужая: сосед по базе уже ушёл вперёд, считать нечего.
        assert await storage.bump_handler_attempts("recorder", 4) == 0
        assert await storage.load_handler_attempts("recorder") == 0
    finally:
        await storage.close()


# --- уборка журнала ----------------------------------------------------------


async def test_pruning_stops_at_the_slowest_handler(settings: Settings) -> None:
    """Уборка не съедает то, чего обработчик ещё не видел."""
    storage = await storage_of(settings)
    try:
        old = Event(
            account_id=1,
            kind="message.received",
            dedup_key="древнее",
            created_at=utcnow() - timedelta(days=120),
        )
        await storage.record_event(old)
        # Обработчик стоит в самом начале: всё, что дальше, — его работа.
        await storage.init_handler_cursor("recorder", 0)

        keeper = Housekeeper(
            storage,
            retention_days=90,
            interval_seconds=3600,
            floor=lambda: storage.handler_cursor_floor(["recorder"]),
        )
        task = asyncio.ensure_future(keeper.run())
        await asyncio.sleep(0.05)
        keeper.stop()
        await task

        assert [item.dedup_key for _, item in await storage.list_events()] == ["древнее"]
    finally:
        await storage.close()


async def test_pruning_follows_the_slowest_of_several_handlers(settings: Settings) -> None:
    """Быстрый обработчик не разрешает убрать то, чего не видел медленный."""
    storage = await storage_of(settings)
    try:
        for number in range(3):
            await storage.record_event(
                Event(
                    account_id=1,
                    kind="message.received",
                    dedup_key=f"старое-{number}",
                    created_at=utcnow() - timedelta(days=120),
                )
            )
        ids = [event_id for event_id, _ in await storage.list_events()]
        await storage.init_handler_cursor("быстрый", ids[-1])
        await storage.init_handler_cursor("медленный", ids[0])

        removed = await storage.prune_events(
            utcnow() - timedelta(days=90),
            keep_from_id=await storage.handler_cursor_floor(["быстрый", "медленный"]),
        )

        # Убрано только то, что медленный уже прошёл.
        assert removed == 1
        assert [event_id for event_id, _ in await storage.list_events()] == ids[1:]
    finally:
        await storage.close()
