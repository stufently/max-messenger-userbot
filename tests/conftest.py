"""Общие приспособления тестов."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

from maxub.config import Settings
from maxub.core.crypto import SecretBox
from maxub.core.service import UserbotService
from maxub.core.storage import Storage
from maxub.transport.stub import STUB_CODE, StubTransport


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Настройки без задержек: тесты не должны ждать лимитер."""
    return Settings(
        data_dir=tmp_path,
        transport="stub",
        send_rate_per_minute=6000.0,
        send_burst=50,
        send_jitter_seconds=0.0,
        retry_base_seconds=0.05,
        retry_max_seconds=0.1,
    )


def build_service(settings: Settings) -> UserbotService:
    return UserbotService(
        settings,
        Storage(settings.db_path, SecretBox(settings.resolve_secret_key())),
        StubTransport,
    )


async def login_by_phone(service: UserbotService, phone: str = "+79990000000") -> int:
    account = await service.add_account(phone)
    challenge_id = await service.start_login(account.id)
    await service.complete_login(challenge_id, STUB_CODE)
    return account.id


async def wait_for(condition: Callable[[], Awaitable[bool]], timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if await condition():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("условие не выполнилось за отведённое время")
