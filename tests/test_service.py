"""Тесты ядра на заглушечном транспорте."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from maxub.config import Settings
from maxub.core.crypto import SecretBox
from maxub.core.models import AccountState, OutboxState
from maxub.core.service import ServiceError, UserbotService
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


@pytest.fixture
async def service(settings: Settings):
    svc = UserbotService(
        settings, Storage(settings.db_path, SecretBox(settings.resolve_secret_key())), StubTransport
    )
    await svc.start()
    try:
        yield svc
    finally:
        await svc.stop()


async def _login(service: UserbotService, phone: str = "+79990000000") -> int:
    account = await service.add_account(phone, label="test")
    challenge_id = await service.start_login(account.id)
    await service.complete_login(challenge_id, STUB_CODE)
    return account.id


async def _wait_state(service: UserbotService, item_id: int, state: OutboxState) -> None:
    for _ in range(100):
        item = await service._storage.get_outbox(item_id)
        if item is not None and item.state is state:
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"элемент {item_id} не дошёл до состояния {state}")


async def test_login_makes_account_ready(service: UserbotService) -> None:
    account_id = await _login(service)
    accounts = await service.list_accounts()
    assert accounts[0].id == account_id
    assert accounts[0].state is AccountState.READY


async def test_wrong_code_keeps_account_unauthorized(service: UserbotService) -> None:
    account = await service.add_account("+79990000001")
    challenge_id = await service.start_login(account.id)
    with pytest.raises(ServiceError):
        await service.complete_login(challenge_id, "99999")
    stored = (await service.list_accounts())[0]
    assert stored.state is AccountState.AUTH_REQUIRED


async def test_duplicate_account_rejected(service: UserbotService) -> None:
    await service.add_account("+79990000002")
    with pytest.raises(ServiceError):
        await service.add_account("+79990000002")


async def test_send_requires_ready_account(service: UserbotService) -> None:
    account = await service.add_account("+79990000003")
    with pytest.raises(ServiceError):
        await service.enqueue_message(account.id, "chat-1", "привет")


async def test_message_is_sent(service: UserbotService) -> None:
    account_id = await _login(service)
    item, created = await service.enqueue_message(account_id, "chat-1", "привет")
    assert created is True
    await _wait_state(service, item.id, OutboxState.SENT)
    stored = await service._storage.get_outbox(item.id)
    assert stored is not None
    assert stored.remote_message_id is not None


async def test_identical_message_is_deduplicated(service: UserbotService) -> None:
    account_id = await _login(service)
    first, created_first = await service.enqueue_message(account_id, "chat-1", "дубль")
    second, created_second = await service.enqueue_message(account_id, "chat-1", "дубль")
    assert created_first is True
    assert created_second is False
    assert first.id == second.id


async def test_nonce_allows_intentional_repeat(service: UserbotService) -> None:
    account_id = await _login(service)
    first, _ = await service.enqueue_message(account_id, "chat-1", "повтор", nonce="1")
    second, created = await service.enqueue_message(account_id, "chat-1", "повтор", nonce="2")
    assert created is True
    assert first.id != second.id


async def test_history_returns_sent_messages(service: UserbotService) -> None:
    account_id = await _login(service)
    item, _ = await service.enqueue_message(account_id, "chat-7", "в историю")
    await _wait_state(service, item.id, OutboxState.SENT)
    history = await service.fetch_history(account_id, "chat-7", limit=10)
    assert [m["text"] for m in history] == ["в историю"]


async def test_events_are_recorded_and_deduplicated(service: UserbotService) -> None:
    account_id = await _login(service)
    item, _ = await service.enqueue_message(account_id, "chat-1", "событие")
    await _wait_state(service, item.id, OutboxState.SENT)
    events = await service.recent_events(limit=50, after_id=0)
    kinds = [e["kind"] for e in events]
    assert "account.ready" in kinds
    assert kinds.count("message.sent") == 1


async def test_disable_stops_account(service: UserbotService) -> None:
    account_id = await _login(service)
    account = await service.disable_account(account_id, "тест")
    assert account.state is AccountState.DISABLED
    with pytest.raises(ServiceError):
        await service.enqueue_message(account_id, "chat-1", "после остановки")
