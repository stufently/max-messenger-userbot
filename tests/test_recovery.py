"""Тесты восстановления после сбоя, дедупликации и прав доступа.

Сценарии из ревью: падение между отправкой и записью результата, повторный
захват очереди двумя воркерами, входящие события после переподключения.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from maxub.config import Settings
from maxub.core import service as service_module
from maxub.core.models import AccountState, OutboxState
from maxub.core.service import UserbotService
from maxub.core.storage import Storage
from maxub.transport.stub import STUB_CODE, StubTransport


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path,
        transport="stub",
        send_rate_per_minute=6000.0,
        send_burst=50,
        send_jitter_seconds=0.0,
    )


def _new_service(settings: Settings) -> UserbotService:
    return UserbotService(settings, Storage(settings.db_path), StubTransport)


async def _login(service: UserbotService, phone: str = "+79990000000") -> int:
    account = await service.add_account(phone)
    challenge_id = await service.start_login(account.id)
    await service.complete_login(challenge_id, STUB_CODE)
    return account.id


async def _wait(condition, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if await condition():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("условие не выполнилось за отведённое время")


async def test_stale_sending_is_failed_not_resent(settings: Settings) -> None:
    """Запись, застрявшая в sending, не переотправляется вслепую."""
    storage = Storage(settings.db_path)
    await storage.open()
    account = await storage.add_account("+79990000000", None)
    await storage.enqueue(account.id, "chat-1", "в полёте", "ключ-1", 60.0)
    claimed = await storage.claim_queued()
    assert claimed[0].state is OutboxState.SENDING
    await storage.close()

    service = _new_service(settings)
    await service.start()
    try:
        item = await service._storage.get_outbox_by_key("ключ-1")
        assert item is not None
        assert item.state is OutboxState.FAILED
        assert "исход отправки неизвестен" in (item.error or "")
    finally:
        await service.stop()


async def test_claim_is_atomic(settings: Settings) -> None:
    """Два одновременных захвата не получают одну и ту же запись."""
    storage = Storage(settings.db_path)
    await storage.open()
    account = await storage.add_account("+79990000000", None)
    for index in range(10):
        await storage.enqueue(account.id, "chat-1", f"сообщение {index}", f"ключ-{index}", 60.0)

    first, second = await asyncio.gather(storage.claim_queued(20), storage.claim_queued(20))
    ids = [item.id for item in first] + [item.id for item in second]
    assert len(ids) == 10
    assert len(set(ids)) == 10
    await storage.close()


async def test_incoming_events_are_recorded_and_deduplicated(settings: Settings) -> None:
    """Входящие сообщения попадают в журнал, повтор того же id — не создаёт дубль."""
    service = _new_service(settings)
    await service.start()
    try:
        account_id = await _login(service)
        transport = service._transports[account_id]
        assert isinstance(transport, StubTransport)

        message = await transport.push_incoming("chat-5", "входящее")

        async def recorded() -> bool:
            events = await service.recent_events(limit=50, after_id=0)
            return any(e["kind"] == "message.received" for e in events)

        await _wait(recorded)

        # Тот же remote_id после «переподключения» не должен дублироваться.
        await transport._incoming.put(message)
        await asyncio.sleep(0.3)
        events = await service.recent_events(limit=50, after_id=0)
        received = [e for e in events if e["kind"] == "message.received"]
        assert len(received) == 1
    finally:
        await service.stop()


async def test_dedup_window_allows_later_repeat(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Без nonce повтор блокируется только в пределах окна, а не навсегда."""
    monkeypatch.setattr(service_module, "DEDUP_WINDOW_SECONDS", 0.0)
    service = _new_service(settings)
    await service.start()
    try:
        account_id = await _login(service)
        first, created_first = await service.enqueue_message(account_id, "chat-1", "повтор")
        second, created_second = await service.enqueue_message(account_id, "chat-1", "повтор")
        assert created_first is True
        assert created_second is True
        assert first.id != second.id
    finally:
        await service.stop()


async def test_nonce_dedups_regardless_of_window(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """С явным nonce клиент управляет идемпотентностью, окно не действует."""
    monkeypatch.setattr(service_module, "DEDUP_WINDOW_SECONDS", 0.0)
    service = _new_service(settings)
    await service.start()
    try:
        account_id = await _login(service)
        first, _ = await service.enqueue_message(account_id, "chat-1", "раз", nonce="n-1")
        second, created = await service.enqueue_message(account_id, "chat-1", "раз", nonce="n-1")
        assert created is False
        assert first.id == second.id
    finally:
        await service.stop()


async def test_secrets_are_not_world_readable(settings: Settings) -> None:
    """В БД лежат сессии аккаунтов — посторонние не должны её читать."""
    service = _new_service(settings)
    await service.start()
    try:
        await _login(service)
        assert settings.data_dir.stat().st_mode & 0o077 == 0
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(f"{settings.db_path}{suffix}")
            if candidate.exists():
                assert candidate.stat().st_mode & 0o077 == 0, candidate
        settings.resolve_token()
        assert settings.token_path.stat().st_mode & 0o077 == 0
    finally:
        await service.stop()


async def test_disabled_account_is_not_resumed(settings: Settings) -> None:
    """Остановку вручную рестарт демона отменять не должен."""
    service = _new_service(settings)
    await service.start()
    account_id = await _login(service)
    await service.disable_account(account_id, "тест")
    await service.stop()

    restarted = _new_service(settings)
    await restarted.start()
    try:
        accounts = await restarted.list_accounts()
        assert accounts[0].state is AccountState.DISABLED
    finally:
        await restarted.stop()


async def test_ready_account_is_resumed_after_restart(settings: Settings) -> None:
    """Готовый аккаунт поднимается сам при следующем старте демона."""
    service = _new_service(settings)
    await service.start()
    await _login(service)
    await service.stop()

    restarted = _new_service(settings)
    await restarted.start()
    try:
        accounts = await restarted.list_accounts()
        assert accounts[0].state is AccountState.READY
    finally:
        await restarted.stop()
