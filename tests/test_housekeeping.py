"""Тесты уборки журнала событий.

Демон рассчитан на месяцы работы, а журнал пишется на каждое событие и сам не
сжимается. Проверяется и то, что старое действительно удаляется, и то, что
уборка не трогает лишнего и не роняет демон, если у неё что-то не получилось.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest

from maxub.config import Settings
from maxub.core.crypto import SecretBox
from maxub.core.housekeeping import Housekeeper
from maxub.core.models import Event, utcnow
from maxub.core.storage import Storage


async def _storage(settings: Settings) -> Storage:
    storage = Storage(settings.db_path, SecretBox(settings.resolve_secret_key()))
    await storage.open()
    return storage


def _event(dedup_key: str, age_days: float) -> Event:
    return Event(
        account_id=1,
        kind="message.received",
        dedup_key=dedup_key,
        created_at=utcnow() - timedelta(days=age_days),
    )


async def test_prune_removes_only_the_old(settings: Settings) -> None:
    storage = await _storage(settings)
    try:
        await storage.record_event(_event("древнее", age_days=120))
        await storage.record_event(_event("вчерашнее", age_days=1))

        removed = await storage.prune_events(utcnow() - timedelta(days=90))

        assert removed == 1
        assert [event.dedup_key for _, event in await storage.list_events()] == ["вчерашнее"]
    finally:
        await storage.close()


async def test_cursor_survives_pruning(settings: Settings) -> None:
    """Курсор `after_id` подрезкой не сбивается: идентификаторы не переиспользуются."""
    storage = await _storage(settings)
    try:
        await storage.record_event(_event("древнее", age_days=120))
        await storage.record_event(_event("вчерашнее", age_days=1))
        old_id = (await storage.list_events())[0][0]

        await storage.prune_events(utcnow() - timedelta(days=90))

        assert [key for key, _ in await storage.list_events(after_id=old_id)] == [old_id + 1]
    finally:
        await storage.close()


class FakeJournal:
    """Журнал, который считает обращения к себе и умеет отказывать."""

    def __init__(self, fail_times: int = 0) -> None:
        self.calls: list[datetime] = []
        self.called = asyncio.Event()
        self._fail_times = fail_times

    async def prune_events(self, older_than: datetime) -> int:
        self.calls.append(older_than)
        self.called.set()
        if self._fail_times > 0:
            self._fail_times -= 1
            raise RuntimeError("база занята")
        return 0


async def _run_until_called(keeper: Housekeeper, journal: FakeJournal, calls: int = 1) -> None:
    task = asyncio.ensure_future(keeper.run())
    try:
        async with asyncio.timeout(2.0):
            while len(journal.calls) < calls:
                await journal.called.wait()
                journal.called.clear()
    finally:
        keeper.stop()
        await task


async def test_first_pass_happens_immediately() -> None:
    """Ждать сутки до первой уборки незачем: демон могли не запускать неделями."""
    journal = FakeJournal()

    await _run_until_called(Housekeeper(journal, retention_days=90, interval_seconds=3600), journal)

    assert journal.calls
    assert (utcnow() - journal.calls[0]).days == 90


async def test_failed_pass_does_not_stop_the_loop() -> None:
    """Сбой базы стоит пропущенного прохода, а не остановки демона."""
    journal = FakeJournal(fail_times=1)

    await _run_until_called(
        Housekeeper(journal, retention_days=90, interval_seconds=0.01), journal, calls=2
    )

    assert len(journal.calls) >= 2


async def test_stop_does_not_wait_out_the_interval() -> None:
    """Остановка не упирается в сутки сна: ожидание идёт через событие."""
    journal = FakeJournal()
    keeper = Housekeeper(journal, retention_days=90, interval_seconds=24 * 3600)
    task = asyncio.ensure_future(keeper.run())
    await asyncio.sleep(0.05)

    keeper.stop()
    async with asyncio.timeout(1.0):
        await task

    assert task.done()


@pytest.mark.parametrize("retention", [-1, 0])
async def test_non_positive_retention_prunes_nothing(retention: int) -> None:
    """Ноль — это «не подрезать»: журнал целиком остаётся на месте."""
    journal = FakeJournal()
    keeper = Housekeeper(journal, retention_days=retention, interval_seconds=3600)

    task = asyncio.ensure_future(keeper.run())
    await asyncio.sleep(0.05)
    keeper.stop()
    await task

    assert journal.calls == []
