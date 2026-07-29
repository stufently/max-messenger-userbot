"""Тесты локального API поверх заглушечного транспорта."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from maxub.api.app import create_app
from maxub.config import Settings
from maxub.transport.stub import STUB_CODE


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    settings = Settings(
        data_dir=tmp_path,
        transport="stub",
        send_rate_per_minute=6000.0,
        send_burst=50,
        send_jitter_seconds=0.0,
    )
    app = create_app(settings)
    with TestClient(app) as test_client:
        test_client.headers.update({"Authorization": f"Bearer {app.state.api_token}"})
        yield test_client


def test_health_needs_no_token(tmp_path: Path) -> None:
    app = create_app(Settings(data_dir=tmp_path, transport="stub"))
    with TestClient(app) as anonymous:
        assert anonymous.get("/health").json() == {"status": "ok"}


def test_status_requires_token(tmp_path: Path) -> None:
    app = create_app(Settings(data_dir=tmp_path, transport="stub"))
    with TestClient(app) as anonymous:
        assert anonymous.get("/status").status_code == 401


def test_wrong_token_rejected(client: TestClient) -> None:
    response = client.get("/status", headers={"Authorization": "Bearer wrong"})
    assert response.status_code == 401


def test_account_lifecycle(client: TestClient) -> None:
    created = client.post("/accounts", json={"phone": "+79990000000", "label": "main"})
    assert created.status_code == 201
    account_id = created.json()["id"]
    assert created.json()["state"] == "auth_required"

    duplicate = client.post("/accounts", json={"phone": "+79990000000"})
    assert duplicate.status_code == 409

    started = client.post("/login/start", json={"account_id": account_id})
    assert started.status_code == 200
    challenge_id = started.json()["challenge_id"]

    completed = client.post(
        "/login/complete", json={"challenge_id": challenge_id, "code": STUB_CODE}
    )
    assert completed.status_code == 200
    assert completed.json()["state"] == "ready"

    status = client.get("/status").json()
    assert status["accounts_ready"] == 1
    assert status["transport"] == "stub"


def test_send_returns_queue_receipt(client: TestClient) -> None:
    account_id = _ready_account(client)
    response = client.post(
        "/send", json={"account_id": account_id, "chat_id": "chat-1", "text": "привет"}
    )
    assert response.status_code == 202
    body = response.json()
    assert body["queued"] is True
    assert body["item"]["state"] in {"queued", "sending", "sent"}


def test_send_to_unauthorized_account_conflicts(client: TestClient) -> None:
    created = client.post("/accounts", json={"phone": "+79990000009"})
    account_id = created.json()["id"]
    response = client.post(
        "/send", json={"account_id": account_id, "chat_id": "chat-1", "text": "нет"}
    )
    assert response.status_code == 409


def test_bad_login_code_is_client_error(client: TestClient) -> None:
    account_id = client.post("/accounts", json={"phone": "+79990000010"}).json()["id"]
    challenge_id = client.post("/login/start", json={"account_id": account_id}).json()[
        "challenge_id"
    ]
    response = client.post("/login/complete", json={"challenge_id": challenge_id, "code": "11111"})
    assert response.status_code == 400


def test_events_expose_cursor(client: TestClient) -> None:
    _ready_account(client)
    events = client.get("/events", params={"limit": 50}).json()
    assert events
    assert events[0]["kind"] == "account.ready"
    after = client.get("/events", params={"after_id": events[-1]["id"]}).json()
    assert after == []


def _ready_account(client: TestClient, phone: str = "+79990000001") -> int:
    account_id = client.post("/accounts", json={"phone": phone}).json()["id"]
    challenge_id = client.post("/login/start", json={"account_id": account_id}).json()[
        "challenge_id"
    ]
    client.post("/login/complete", json={"challenge_id": challenge_id, "code": STUB_CODE})
    return int(account_id)
