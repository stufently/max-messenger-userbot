"""Тесты областей доступа: выпуск токенов, отказы и их коды.

Проверяется главное обещание: токен, выданный на чтение, не должен уметь ни
отправлять сообщения, ни выпускать себе новые права — ни через API, ни через
панель, ни через живой поток событий.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from maxub.api.app import create_app
from maxub.config import Settings
from maxub.core.access import AccessControl, fingerprint
from maxub.core.crypto import SecretBox
from maxub.core.models import utcnow
from maxub.core.permissions import ALL_SCOPES, Scope, UnknownScopeError, parse_scopes
from maxub.core.storage import Storage

READ_ONLY = [Scope.ACCOUNTS_READ.value]


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    app = create_app(Settings(data_dir=tmp_path, transport="stub", send_jitter_seconds=0.0))
    # Петлевой адрес: панель отвергает обращение по чужому имени, а базовый
    # `testserver` был бы как раз чужим.
    with TestClient(app, base_url="http://127.0.0.1:8765") as test_client:
        test_client.headers.update({"Authorization": f"Bearer {app.state.api_token}"})
        yield test_client


def issue(client: TestClient, scopes: list[str], **extra: object) -> str:
    response = client.post("/tokens", json={"label": "проверка", "scopes": scopes, **extra})
    assert response.status_code == 201, response.text
    return str(response.json()["token"])


def as_token(client: TestClient, token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# --- разбор областей ---------------------------------------------------------


def test_unknown_scope_is_rejected() -> None:
    with pytest.raises(UnknownScopeError):
        parse_scopes(["messages:wrte"])


def test_scopes_round_trip() -> None:
    assert parse_scopes([scope.value for scope in ALL_SCOPES]) == ALL_SCOPES


# --- выпуск и отзыв ----------------------------------------------------------


def test_issued_token_works_within_its_scopes(client: TestClient) -> None:
    token = issue(client, READ_ONLY)
    headers = as_token(client, token)

    assert client.get("/accounts", headers=headers).status_code == 200
    # Права ровно те, что выданы: добавить аккаунт нельзя.
    forbidden = client.post("/accounts", json={"phone": "+79990000001"}, headers=headers)
    assert forbidden.status_code == 403
    assert "accounts:write" in forbidden.json()["detail"]


def test_token_value_is_shown_once_and_not_stored(client: TestClient, tmp_path: Path) -> None:
    token = issue(client, READ_ONLY)
    listing = client.get("/tokens").json()

    assert len(listing) == 1
    assert token not in repr(listing)
    assert listing[0]["label"] == "проверка"


def test_revoked_token_stops_working_at_once(client: TestClient) -> None:
    token = issue(client, READ_ONLY)
    token_id = client.get("/tokens").json()[0]["id"]

    assert client.delete(f"/tokens/{token_id}").status_code == 200
    assert client.get("/accounts", headers=as_token(client, token)).status_code == 401
    # Повторный отзыв — уже нечего отзывать.
    assert client.delete(f"/tokens/{token_id}").status_code == 404


def test_expired_token_is_not_accepted(client: TestClient, tmp_path: Path) -> None:
    token = issue(client, READ_ONLY, expires_in_days=1)
    storage: Storage = client.app.state.service._storage  # type: ignore[attr-defined]

    async def expire() -> None:
        row = await storage.find_token_by_hash(fingerprint(token))
        assert row is not None
        async with storage.write() as db:
            await db.execute(
                "UPDATE api_tokens SET expires_at = ? WHERE id = ?",
                ((utcnow().replace(year=utcnow().year - 1)).isoformat(), row.id),
            )

    client.portal.call(expire)  # type: ignore[union-attr]
    assert client.get("/accounts", headers=as_token(client, token)).status_code == 401


def test_scope_cannot_be_escalated_by_its_holder(client: TestClient) -> None:
    """Право выпускать токены не должно превращаться в право на всё."""
    admin_only = issue(client, [Scope.ADMIN.value])

    response = client.post(
        "/tokens",
        json={"label": "себе побольше", "scopes": [Scope.MESSAGES_WRITE.value]},
        headers=as_token(client, admin_only),
    )

    assert response.status_code == 403
    assert "messages:write" in response.json()["detail"]


def test_unknown_scope_in_request_is_a_bad_request(client: TestClient) -> None:
    response = client.post("/tokens", json={"label": "опечатка", "scopes": ["messages:wrte"]})
    assert response.status_code == 422


def test_token_without_admin_cannot_manage_tokens(client: TestClient) -> None:
    token = issue(client, READ_ONLY)
    headers = as_token(client, token)

    assert client.get("/tokens", headers=headers).status_code == 403
    assert (
        client.post(
            "/tokens", json={"label": "x", "scopes": READ_ONLY}, headers=headers
        ).status_code
        == 403
    )


# --- маршруты ----------------------------------------------------------------


def test_journal_needs_the_right_to_read_messages(client: TestClient) -> None:
    """`events:read` не должен становиться обходным путём к текстам сообщений."""
    watcher = as_token(client, issue(client, [Scope.EVENTS_READ.value]))

    assert client.get("/events", headers=watcher).status_code == 403

    reader = as_token(client, issue(client, [Scope.EVENTS_READ.value, Scope.MESSAGES_READ.value]))
    assert client.get("/events", headers=reader).status_code == 200


def test_shutdown_is_admin_only(client: TestClient) -> None:
    headers = as_token(client, issue(client, [Scope.ACCOUNTS_READ.value]))
    assert client.post("/shutdown", headers=headers).status_code == 403


def test_websocket_tells_apart_the_unknown_and_the_forbidden(client: TestClient) -> None:
    """Живой поток отвечает разными кодами: 4401 — «кто вы», 4403 — «нельзя»."""
    with pytest.raises(WebSocketDisconnect) as unknown:
        with client.websocket_connect("/ws/events", headers={"Authorization": "Bearer wrong"}):
            pass
    assert unknown.value.code == 4401

    token = issue(client, [Scope.ACCOUNTS_READ.value])
    with pytest.raises(WebSocketDisconnect) as forbidden:
        with client.websocket_connect("/ws/events", headers=as_token(client, token)):
            pass
    assert forbidden.value.code == 4403


def test_root_token_keeps_all_rights(client: TestClient) -> None:
    """Совместимость: у того, кто пользуется файловым токеном, ничего не менялось."""
    assert client.get("/accounts").status_code == 200
    assert client.get("/tokens").status_code == 200
    assert client.get("/events").status_code == 200


# --- панель ------------------------------------------------------------------


def test_web_session_inherits_the_token_rights(client: TestClient) -> None:
    token = issue(client, READ_ONLY)

    opened = client.post("/web/session", json={"token": token})
    assert opened.status_code == 200
    csrf = opened.json()["csrf"]

    assert client.get("/web/api/state").status_code == 200
    added = client.post(
        "/web/api/accounts", json={"phone": "+79990000002"}, headers={"X-CSRF-Token": csrf}
    )
    assert added.status_code == 403


def test_revoking_the_token_closes_the_panel(client: TestClient) -> None:
    token = issue(client, [Scope.ACCOUNTS_READ.value])
    assert client.post("/web/session", json={"token": token}).status_code == 200
    token_id = client.get("/tokens").json()[0]["id"]

    assert client.delete(f"/tokens/{token_id}").status_code == 200

    # Сессия живёт двенадцать часов, но право на неё кончилось сейчас.
    assert client.get("/web/api/state").status_code == 401


def test_handoff_code_carries_the_rights_of_its_issuer(client: TestClient) -> None:
    token = issue(client, READ_ONLY)
    code = client.post("/web/handoff", headers=as_token(client, token)).json()["code"]

    client.get("/web/enter", params={"code": code}, follow_redirects=False)
    session = client.get("/web/session").json()

    assert session["authenticated"] is True
    assert session["scopes"] == Scope.ACCOUNTS_READ.value
    assert (
        client.post(
            "/web/api/accounts",
            json={"phone": "+79990000003"},
            headers={"X-CSRF-Token": session["csrf"]},
        ).status_code
        == 403
    )


# --- хранилище ---------------------------------------------------------------


async def test_use_is_marked_no_more_than_once_a_minute(settings: Settings) -> None:
    storage = Storage(settings.db_path, SecretBox(settings.resolve_secret_key()))
    await storage.open()
    try:
        access = AccessControl(storage, "корневой-секрет")
        raw, token = await access.issue("скрипт", frozenset({Scope.ACCOUNTS_READ}))

        assert await access.authenticate(raw) is not None
        first = (await storage.get_token(token.id)).last_used_at  # type: ignore[union-attr]
        assert first is not None

        assert await access.authenticate(raw) is not None
        assert (await storage.get_token(token.id)).last_used_at == first  # type: ignore[union-attr]
    finally:
        await storage.close()
