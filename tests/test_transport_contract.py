"""Общие требования контракта — для каждого зарегистрированного транспорта.

Заявленные возможности и поведение проверяются одинаково у всех адаптеров:
иначе второй транспорт молча разошёлся бы с первым, а узнали бы об этом на
живом аккаунте. Проверяется не то, что адаптер умеет, а то, что он не врёт про
умения и не выдумывает того, чего не знает.

Подготовка у каждого транспорта своя: заглушке хватает своей сессии, адаптеру
PyMax нужен подменный модуль и вход. Дальше идут одни и те же утверждения.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any

import pytest

from maxub.core.models import Session
from maxub.transport import available, get_factory
from maxub.transport.base import ReconcileOutcome, Transport, TransportUnsupported
from maxub.transport.stub import StubTransport
from tests.test_transport_pymax import FakeMessage, install_fake_pymax


@dataclass(slots=True)
class Harness:
    """Подготовленный транспорт и способ подсунуть ему входящее сообщение."""

    transport: Any
    deliver: Callable[[], Any]


async def _stub_harness(monkeypatch: pytest.MonkeyPatch) -> Harness:
    transport = StubTransport()
    await transport.connect(Session(account_id=1, phone="+7", token="stub-x", device_id="d"))
    return Harness(transport, lambda: transport.push_incoming("42", "привет"))


async def _pymax_harness(monkeypatch: pytest.MonkeyPatch) -> Harness:
    server = install_fake_pymax(monkeypatch)
    transport = get_factory("pymax")()
    challenge = await transport.start_login("+79990000000")
    session = await transport.complete_login(challenge.challenge_id, server.valid_code, 1)
    await transport.connect(session)
    client = server.clients[-1]
    return Harness(transport, lambda: client.deliver(FakeMessage(7, 42, sender=99, text="привет")))


HARNESSES = {"stub": _stub_harness, "pymax": _pymax_harness}


@pytest.fixture(params=sorted(HARNESSES))
async def harness(request: Any, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Harness]:
    prepared = await HARNESSES[request.param](monkeypatch)
    try:
        yield prepared
    finally:
        await prepared.transport.disconnect()


def test_registry_lists_both_transports() -> None:
    assert available() == ["pymax", "stub"]


async def test_transport_satisfies_protocol(harness: Harness) -> None:
    assert isinstance(harness.transport, Transport)


async def test_disabled_abilities_refuse_loudly(harness: Harness) -> None:
    """Выключенная возможность отказывает явно, а не отвечает пустотой."""
    transport = harness.transport
    if not transport.capabilities.backfill:
        with pytest.raises(TransportUnsupported):
            await transport.fetch_updates(None, 10)
    if not transport.capabilities.qr_login:
        with pytest.raises(TransportUnsupported):
            await transport.start_qr_login()


async def test_absence_is_never_invented(harness: Harness) -> None:
    """Без возможности сверки `NOT_FOUND` не выдаётся ни при каких условиях.

    Это самое дорогое утверждение набора: `NOT_FOUND` разрешает ядру повтор, и
    неверный ответ здесь оплачивается дублем у получателя.
    """
    transport = harness.transport
    if transport.capabilities.reconcile:
        pytest.skip("транспорт умеет сверку, отсутствие доказуемо")
    result = await transport.reconcile_send("42", "никогда-не-отправлялся")
    assert result.outcome is ReconcileOutcome.INCONCLUSIVE


async def test_cursor_lives_in_one_space(harness: Harness) -> None:
    """Позиция из живого потока годится для добора — или её нет вовсе.

    Подмена позиции идентификатором сообщения не всплыла бы ни в одном другом
    тесте: курсор просто уехал бы в чужую систему координат и добор молча
    перестал бы находить пропущенное.
    """
    transport = harness.transport
    await harness.deliver()
    update = await asyncio.wait_for(anext(transport.events()), timeout=1)
    if update.cursor is None:
        assert not transport.capabilities.backfill
        return
    assert update.cursor != update.message.remote_id
    # Позиция принимается добором и не отдаёт то же событие второй раз.
    updates, _ = await transport.fetch_updates(update.cursor, 10)
    assert all(item.message.remote_id != update.message.remote_id for item in updates)
