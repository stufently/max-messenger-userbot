"""Вход по QR-коду — второй способ авторизации, без SMS."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from maxub.api.app import create_app
from maxub.config import Settings
from maxub.core.auth import LoginError
from maxub.core.challenges import (
    MAX_ACTIVE_CHALLENGES,
    MAX_CHALLENGE_TTL,
    ChallengeRegistry,
)
from maxub.core.models import AccountState, QrStatus, utcnow
from maxub.core.service import ServiceError, UserbotService
from maxub.transport import stub
from maxub.transport.stub import StubTransport
from tests.conftest import build_service


@pytest.fixture
async def service(settings: Settings) -> Iterator[UserbotService]:
    svc = build_service(settings)
    await svc.start()
    try:
        yield svc
    finally:
        await svc.stop()


async def test_qr_login_makes_account_ready(service: UserbotService) -> None:
    account = await service.add_account("+79990000000")
    challenge = await service.start_qr_login(account.id)
    assert challenge["payload"].startswith("maxub-stub-qr://")

    status, pending_account = await service.poll_qr_login(challenge["challenge_id"])
    assert status is QrStatus.PENDING
    assert pending_account is None

    transport = service._connections.get(account.id)
    assert isinstance(transport, StubTransport)
    transport.confirm_qr(challenge["challenge_id"])

    status, ready_account = await service.poll_qr_login(challenge["challenge_id"])
    assert status is QrStatus.CONFIRMED
    assert ready_account is not None
    assert ready_account.state is AccountState.READY


async def test_qr_poll_rejects_unknown_challenge(service: UserbotService) -> None:
    with pytest.raises(ServiceError):
        await service.poll_qr_login("нет-такого")


async def test_qr_challenge_is_single_use(service: UserbotService) -> None:
    account = await service.add_account("+79990000001")
    challenge = await service.start_qr_login(account.id)
    transport = service._connections.get(account.id)
    assert isinstance(transport, StubTransport)
    transport.confirm_qr(challenge["challenge_id"])

    await service.poll_qr_login(challenge["challenge_id"])
    with pytest.raises(ServiceError):
        await service.poll_qr_login(challenge["challenge_id"])


async def test_qr_login_needs_no_phone_code(service: UserbotService) -> None:
    """QR-вход не обращается к коду подтверждения вовсе."""
    account = await service.add_account("+79990000002")
    challenge = await service.start_qr_login(account.id)
    transport = service._connections.get(account.id)
    assert isinstance(transport, StubTransport)
    assert transport._challenges == {}
    transport.confirm_qr(challenge["challenge_id"])
    status, _ = await service.poll_qr_login(challenge["challenge_id"])
    assert status is QrStatus.CONFIRMED


# --- срок жизни и одновременные обращения ------------------------------------


async def test_expired_qr_challenge_reports_expired(
    service: UserbotService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Истёкший запрос — это «expired», а не «неизвестный запрос».

    Срок берётся у транспорта, поэтому истечение имитируется нулевым TTL
    заглушки, а не подменой часов ядра.
    """
    monkeypatch.setattr(stub, "CHALLENGE_TTL", timedelta(0))
    account = await service.add_account("+79990000005")
    challenge = await service.start_qr_login(account.id)

    status, expired_account = await service.poll_qr_login(challenge["challenge_id"])
    assert status is QrStatus.EXPIRED
    assert expired_account is None


async def test_new_qr_challenge_invalidates_previous(service: UserbotService) -> None:
    """Второй запрос отменяет первый: пользователю нужен только свежий код."""
    account = await service.add_account("+79990000006")
    first = await service.start_qr_login(account.id)
    second = await service.start_qr_login(account.id)
    assert second["challenge_id"] != first["challenge_id"]

    transport = service._connections.get(account.id)
    assert isinstance(transport, StubTransport)
    transport.confirm_qr(str(first["challenge_id"]))
    status, stale_account = await service.poll_qr_login(str(first["challenge_id"]))
    assert status is QrStatus.EXPIRED
    assert stale_account is None

    transport.confirm_qr(str(second["challenge_id"]))
    status, ready_account = await service.poll_qr_login(str(second["challenge_id"]))
    assert status is QrStatus.CONFIRMED
    assert ready_account is not None


async def test_concurrent_polls_confirm_login_once(service: UserbotService) -> None:
    """Веб-интерфейс и CLI могут опрашивать один запрос одновременно."""
    account = await service.add_account("+79990000007")
    challenge = await service.start_qr_login(account.id)
    challenge_id = str(challenge["challenge_id"])
    transport = service._connections.get(account.id)
    assert isinstance(transport, StubTransport)
    transport.confirm_qr(challenge_id)

    polls: list[str] = []
    original = transport.poll_qr_login

    async def slow_poll(cid: str, account_id: int) -> object:
        # Без задержки внутри транспорта гонки не выйдет: заглушка отвечает,
        # ни разу не уступив управление циклу событий.
        polls.append(cid)
        await asyncio.sleep(0.05)
        return await original(cid, account_id)

    transport.poll_qr_login = slow_poll  # type: ignore[assignment]

    results = await asyncio.gather(
        service.poll_qr_login(challenge_id),
        service.poll_qr_login(challenge_id),
        return_exceptions=True,
    )

    confirmed = [
        item
        for item in results
        if not isinstance(item, BaseException) and item[0] is QrStatus.CONFIRMED
    ]
    # Опоздавший получает прикладную ошибку, а не KeyError, и до транспорта
    # не доходит вовсе — иначе вход подтвердился бы дважды.
    failures = [item for item in results if isinstance(item, BaseException)]
    assert len(confirmed) == 1
    assert len(failures) == 1
    assert isinstance(failures[0], ServiceError)
    assert len(polls) == 1


async def test_poll_after_confirmation_is_not_a_second_login(service: UserbotService) -> None:
    account = await service.add_account("+79990000008")
    challenge = await service.start_qr_login(account.id)
    challenge_id = str(challenge["challenge_id"])
    transport = service._connections.get(account.id)
    assert isinstance(transport, StubTransport)
    transport.confirm_qr(challenge_id)

    status, _ = await service.poll_qr_login(challenge_id)
    assert status is QrStatus.CONFIRMED

    with pytest.raises(ServiceError) as failure:
        await service.poll_qr_login(challenge_id)
    assert "использован" in str(failure.value)


async def test_restart_during_poll_does_not_confirm_stale_challenge(
    service: UserbotService,
) -> None:
    """Запрос могут вытеснить, пока он ждёт ответа транспорта."""
    account = await service.add_account("+79990000009")
    challenge = await service.start_qr_login(account.id)
    challenge_id = str(challenge["challenge_id"])
    transport = service._connections.get(account.id)
    assert isinstance(transport, StubTransport)
    transport.confirm_qr(challenge_id)

    original = transport.poll_qr_login

    async def slow_poll(cid: str, account_id: int) -> object:
        await asyncio.sleep(0.05)
        return await original(cid, account_id)

    transport.poll_qr_login = slow_poll  # type: ignore[assignment]

    polling = asyncio.create_task(service.poll_qr_login(challenge_id))
    await asyncio.sleep(0.01)
    await service.start_qr_login(account.id)

    status, stale_account = await polling
    assert status is QrStatus.EXPIRED
    assert stale_account is None


async def test_expiry_during_poll_does_not_confirm(
    service: UserbotService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Срок может истечь прямо во время вызова транспорта."""
    monkeypatch.setattr(stub, "CHALLENGE_TTL", timedelta(seconds=0.05))
    account = await service.add_account("+79990000011")
    challenge = await service.start_qr_login(account.id)
    challenge_id = str(challenge["challenge_id"])
    transport = service._connections.get(account.id)
    assert isinstance(transport, StubTransport)
    transport.confirm_qr(challenge_id)

    original = transport.poll_qr_login

    async def slow_poll(cid: str, account_id: int) -> object:
        await asyncio.sleep(0.2)
        return await original(cid, account_id)

    transport.poll_qr_login = slow_poll  # type: ignore[assignment]

    status, stale_account = await service.poll_qr_login(challenge_id)
    assert status is QrStatus.EXPIRED
    assert stale_account is None


async def test_parallel_starts_leave_one_live_challenge(service: UserbotService) -> None:
    """Два одновременных start_qr не оставляют аккаунту два живых запроса."""
    account = await service.add_account("+79990000010")
    first, second = await asyncio.gather(
        service.start_qr_login(account.id), service.start_qr_login(account.id)
    )
    transport = service._connections.get(account.id)
    assert isinstance(transport, StubTransport)

    statuses = []
    for challenge in (first, second):
        challenge_id = str(challenge["challenge_id"])
        transport.confirm_qr(challenge_id)
        status, _ = await service.poll_qr_login(challenge_id)
        statuses.append(status)
    assert statuses.count(QrStatus.CONFIRMED) == 1
    assert statuses.count(QrStatus.EXPIRED) == 1


# --- реестр запросов ---------------------------------------------------------


def test_registry_caps_lifetime_from_transport() -> None:
    """Неадекватно далёкий срок от транспорта урезается потолком."""
    registry = ChallengeRegistry("запрос")
    registry.add("cid", 1, utcnow() + timedelta(hours=1))
    assert registry.get("cid").expires_at <= utcnow() + MAX_CHALLENGE_TTL


def test_registry_limits_live_challenges() -> None:
    registry = ChallengeRegistry("запрос")
    for index in range(MAX_ACTIVE_CHALLENGES):
        registry.add(f"cid-{index}", index, utcnow() + timedelta(minutes=5))
    with pytest.raises(LoginError):
        registry.prepare(MAX_ACTIVE_CHALLENGES)


# --- через API ---------------------------------------------------------------


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    app = create_app(Settings(data_dir=tmp_path, transport="stub", send_jitter_seconds=0.0))
    with TestClient(app) as test_client:
        test_client.headers.update({"Authorization": f"Bearer {app.state.api_token}"})
        yield test_client


def test_qr_endpoints(client: TestClient) -> None:
    account_id = client.post("/accounts", json={"phone": "+79990000003"}).json()["id"]

    started = client.post("/login/qr/start", json={"account_id": account_id})
    assert started.status_code == 200
    challenge_id = started.json()["challenge_id"]

    pending = client.post("/login/qr/poll", json={"challenge_id": challenge_id})
    assert pending.json()["status"] == "pending"
    assert pending.json()["account"] is None

    service: UserbotService = client.app.state.service  # type: ignore[attr-defined]
    transport = service._connections.get(account_id)
    assert isinstance(transport, StubTransport)
    transport.confirm_qr(challenge_id)

    confirmed = client.post("/login/qr/poll", json={"challenge_id": challenge_id})
    assert confirmed.json()["status"] == "confirmed"
    assert confirmed.json()["account"]["state"] == "ready"


def test_capabilities_endpoint_reports_qr_support(client: TestClient) -> None:
    account_id = client.post("/accounts", json={"phone": "+79990000004"}).json()["id"]
    caps = client.get(f"/accounts/{account_id}/capabilities").json()
    assert caps["qr_login"] is True
    assert caps["send_text"] is True
