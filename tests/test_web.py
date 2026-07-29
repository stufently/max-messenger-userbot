"""Веб-интерфейс: страница, вход браузера, CSRF и сквозные сценарии входа."""

from __future__ import annotations

import base64
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from maxub.api.app import create_app
from maxub.api.routes.web_handoff import MAX_HANDOFF_CODES, HandoffCodes
from maxub.api.routes.web_session import MAX_SESSIONS, SESSION_COOKIE, WebSession
from maxub.config import Settings
from maxub.core.models import utcnow
from maxub.core.service import UserbotService
from maxub.transport.stub import STUB_CODE, StubTransport


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    """Клиент без заголовка Authorization: браузер токен не носит.

    Адрес именно петлевой: панель отвергает обращение по чужому имени, а
    базовый `testserver` как раз таким и был бы.
    """
    app = create_app(Settings(data_dir=tmp_path, transport="stub", send_jitter_seconds=0.0))
    with TestClient(app, base_url="http://127.0.0.1:8765") as test_client:
        yield test_client


def open_session(client: TestClient) -> str:
    token: str = client.app.state.api_token  # type: ignore[attr-defined]
    response = client.post("/web/session", json={"token": token})
    assert response.status_code == 200
    return str(response.json()["csrf"])


def csrf_headers(csrf: str) -> dict[str, str]:
    return {"X-CSRF-Token": csrf}


def bearer(client: TestClient) -> dict[str, str]:
    token: str = client.app.state.api_token  # type: ignore[attr-defined]
    return {"Authorization": f"Bearer {token}"}


def issue_code(client: TestClient) -> str:
    response = client.post("/web/handoff", headers=bearer(client))
    assert response.status_code == 200
    return str(response.json()["code"])


def enter(client: TestClient, code: str) -> httpx.Response:
    """Переход по адресу с кодом — ровно то, что делает лаунчер."""
    return client.get("/web/enter", params={"code": code}, follow_redirects=False)


# --- страница ----------------------------------------------------------------


def test_page_is_served(client: TestClient) -> None:
    response = client.get("/web")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    body = response.text
    assert "Добавить аккаунт" in body
    assert "/web/static/api.js" in body
    assert "/web/static/session.js" in body
    # Страница — пустая оболочка: секретов в разметке нет.
    assert client.app.state.api_token not in body  # type: ignore[attr-defined]


def test_assets_are_whitelisted(client: TestClient) -> None:
    for name in ("api.js", "accounts.js", "login.js", "session.js", "app.css"):
        assert client.get(f"/web/static/{name}").status_code == 200
    # Отдаются только известные имена, поэтому чужой файл не достать.
    assert client.get("/web/static/api_token").status_code == 404


def test_web_ui_can_be_disabled(tmp_path: Path) -> None:
    app = create_app(Settings(data_dir=tmp_path, transport="stub", web_ui=False))
    with TestClient(app, base_url="http://127.0.0.1:8765") as anonymous:
        paths = ("/web", "/web/static/api.js", "/web/session", "/web/api/state", "/web/enter")
        for path in paths:
            assert anonymous.get(path).status_code == 404
        # Выдача одноразовых кодов исчезает вместе с панелью, а не остаётся
        # висеть отдельным маршрутом.
        assert anonymous.post("/web/handoff").status_code == 404


def test_foreign_host_is_rejected(client: TestClient) -> None:
    """Защита от перепривязки DNS: чужое имя не должно попасть в один origin."""
    for path in ("/web", "/web/session", "/web/api/state"):
        assert client.get(path, headers={"Host": "evil.example"}).status_code == 400
    assert client.get("/web", headers={"Host": "localhost:8765"}).status_code == 200


def test_wildcard_bind_still_checks_host(tmp_path: Path) -> None:
    """`0.0.0.0` — это адрес привязки, а не разрешение принимать любое имя.

    Именно так демон работает при пробросе порта из Docker, и раньше проверка
    в этом режиме отключалась целиком — защита пропадала там, где нужна.
    """
    app = create_app(Settings(data_dir=tmp_path, transport="stub", host="0.0.0.0"))
    with TestClient(app, base_url="http://127.0.0.1:8765") as anonymous:
        assert anonymous.get("/web", headers={"Host": "evil.example"}).status_code == 400
        assert anonymous.get("/web").status_code == 200


def test_extra_hosts_can_be_allowed(tmp_path: Path) -> None:
    """Своё имя разрешается явно, а не побочным эффектом адреса привязки."""
    app = create_app(
        Settings(
            data_dir=tmp_path,
            transport="stub",
            host="0.0.0.0",
            web_allowed_hosts="maxub.local, box.lan",
        )
    )
    with TestClient(app, base_url="http://127.0.0.1:8765") as anonymous:
        assert anonymous.get("/web", headers={"Host": "maxub.local"}).status_code == 200
        assert anonymous.get("/web", headers={"Host": "box.lan:8765"}).status_code == 200
        assert anonymous.get("/web", headers={"Host": "evil.example"}).status_code == 400


def test_security_headers_are_set(client: TestClient) -> None:
    for path in ("/web", "/web/static/api.js", "/web/session"):
        headers = client.get(path).headers
        assert headers["cache-control"] == "no-store"
        assert headers["x-content-type-options"] == "nosniff"
        assert "frame-ancestors 'none'" in headers["content-security-policy"]
        # Картинка QR приходит data-URI, поэтому схема data: разрешена явно.
        assert "img-src 'self' data:" in headers["content-security-policy"]


# --- вход в панель и защита --------------------------------------------------


def test_state_requires_session(client: TestClient) -> None:
    assert client.get("/web/api/state").status_code == 401


def test_wrong_token_gives_no_session(client: TestClient) -> None:
    # Значение намеренно с кириллицей: сравнение секретов не должно падать на
    # нелатинице, введённой в форму.
    response = client.post("/web/session", json={"token": "не тот"})
    assert response.status_code == 401
    assert client.cookies.get(SESSION_COOKIE) is None


def test_session_cookie_is_hardened(client: TestClient) -> None:
    token: str = client.app.state.api_token  # type: ignore[attr-defined]
    response = client.post("/web/session", json={"token": token})
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie
    assert "samesite=strict" in cookie
    # Путь ограничен панелью: на bearer-маршруты cookie не уходит вовсе.
    assert "path=/web" in cookie


def test_api_routes_still_require_bearer_token(client: TestClient) -> None:
    """Веб-вход не должен открывать дыру в основном API."""
    open_session(client)
    sid = client.cookies.get(SESSION_COOKIE)
    assert sid
    for path in ("/status", "/accounts"):
        response = client.get(path, headers={"Cookie": f"{SESSION_COOKIE}={sid}"})
        assert response.status_code == 401


def test_mutations_need_csrf_header(client: TestClient) -> None:
    csrf = open_session(client)
    payload = {"phone": "+79990000100"}
    assert client.post("/web/api/accounts", json=payload).status_code == 403
    wrong = client.post("/web/api/accounts", json=payload, headers=csrf_headers("chuzhaya-metka"))
    assert wrong.status_code == 403
    ok = client.post("/web/api/accounts", json=payload, headers=csrf_headers(csrf))
    assert ok.status_code == 201

    # Метку требует каждый изменяющий маршрут, а не только добавление.
    account_id = ok.json()["id"]
    for path, body in (
        ("/web/api/login/start", {"account_id": account_id}),
        ("/web/api/login/qr/start", {"account_id": account_id}),
        (f"/web/api/accounts/{account_id}/disable", {"reason": "нет"}),
    ):
        assert client.post(path, json=body).status_code == 403
    # Чтение метки не требует: иначе страница не смогла бы её получить.
    assert client.get("/web/api/state").status_code == 200


def test_bogus_cookie_is_not_a_session(client: TestClient) -> None:
    response = client.get("/web/api/state", headers={"Cookie": f"{SESSION_COOKIE}=net-takoy"})
    assert response.status_code == 401


def test_expired_session_is_dropped(client: TestClient) -> None:
    open_session(client)
    store: dict[str, WebSession] = client.app.state.web_sessions  # type: ignore[attr-defined]
    for session in store.values():
        session.expires_at = utcnow() - timedelta(seconds=1)
    assert client.get("/web/api/state").status_code == 401
    # Истёкшая запись не должна оставаться в памяти демона.
    assert store == {}


def test_session_count_is_capped(client: TestClient) -> None:
    token: str = client.app.state.api_token  # type: ignore[attr-defined]
    for _ in range(MAX_SESSIONS + 5):
        assert client.post("/web/session", json={"token": token}).status_code == 200
    store: dict[str, WebSession] = client.app.state.web_sessions  # type: ignore[attr-defined]
    assert len(store) <= MAX_SESSIONS


# --- одноразовый код входа ---------------------------------------------------


def test_token_form_login_still_works(client: TestClient) -> None:
    """Вход по токену в форму — единственный способ там, где нет лаунчера."""
    csrf = open_session(client)
    assert csrf
    assert client.get("/web/api/state").status_code == 200


def test_handoff_code_opens_session(client: TestClient) -> None:
    """Переход по адресу с кодом должен сразу давать рабочую сессию."""
    response = enter(client, issue_code(client))
    assert response.status_code == 303
    assert SESSION_COOKIE in response.headers.get("set-cookie", "")

    info = client.get("/web/session").json()
    assert info["authenticated"] is True
    assert info["csrf"]
    assert client.get("/web/api/state").status_code == 200
    # Метка из обмена работает наравне с выданной по токену.
    added = client.post(
        "/web/api/accounts", json={"phone": "+79990000105"}, headers=csrf_headers(info["csrf"])
    )
    assert added.status_code == 201


def test_handoff_redirect_leaves_no_code_in_address(client: TestClient) -> None:
    """Код не должен остаться в адресной строке и в истории переходов."""
    code = issue_code(client)
    location = enter(client, code).headers["location"]
    assert location == "/web"
    assert code not in location
    assert "?" not in location


def test_handoff_does_not_leak_code_as_referrer(client: TestClient) -> None:
    """Адрес с кодом не должен уехать в `Referer` следующего запроса.

    Перенаправление уводит браузер с `/web/enter?code=…` на `/web`, и без
    запрета браузер приложил бы исходный адрес заголовком — код утёк бы в тот же
    запрос, от которого его прячут.
    """
    code = issue_code(client)
    assert enter(client, code).headers["referrer-policy"] == "no-referrer"


def test_handoff_code_is_single_use(client: TestClient) -> None:
    code = issue_code(client)
    assert enter(client, code).status_code == 303
    # Cookie от первого перехода убираем: проверяем именно повторный обмен, а
    # не выданную ранее сессию.
    client.cookies.clear()
    repeated = enter(client, code)
    assert repeated.status_code == 303
    assert SESSION_COOKIE not in repeated.headers.get("set-cookie", "")
    assert client.get("/web/session").json()["authenticated"] is False
    assert client.get("/web/api/state").status_code == 401


def test_expired_handoff_code_is_rejected(client: TestClient) -> None:
    code = issue_code(client)
    codes: HandoffCodes = client.app.state.web_handoffs  # type: ignore[attr-defined]
    expired, principal = codes._items[code]
    codes._items[code] = (utcnow() - timedelta(seconds=1), principal)
    assert expired > utcnow()
    assert SESSION_COOKIE not in enter(client, code).headers.get("set-cookie", "")
    assert client.get("/web/session").json()["authenticated"] is False
    # Просроченное не копится в памяти демона.
    assert codes._items == {}


def test_unknown_handoff_code_is_rejected(client: TestClient) -> None:
    for code in ("", "net-takogo-koda"):
        response = client.get("/web/enter", params={"code": code}, follow_redirects=False)
        assert response.status_code == 303
        assert SESSION_COOKIE not in response.headers.get("set-cookie", "")
    assert client.get("/web/api/state").status_code == 401


def test_handoff_needs_daemon_token(client: TestClient) -> None:
    """Код выдаётся только обладателю токена — иначе он обходил бы вход."""
    assert client.post("/web/handoff").status_code == 401
    wrong = client.post("/web/handoff", headers={"Authorization": "Bearer chuzhoj-token"})
    assert wrong.status_code == 401
    # Сессия панели правом на выдачу тоже не является: cookie сюда не пускает.
    csrf = open_session(client)
    assert client.post("/web/handoff", headers=csrf_headers(csrf)).status_code == 401


def test_handoff_codes_are_capped(client: TestClient) -> None:
    for _ in range(MAX_HANDOFF_CODES + 5):
        issue_code(client)
    codes: HandoffCodes = client.app.state.web_handoffs  # type: ignore[attr-defined]
    assert len(codes._items) <= MAX_HANDOFF_CODES


def test_handoff_response_is_not_cached(client: TestClient) -> None:
    """Перенаправление с `Set-Cookie` кэшировать нельзя."""
    response = enter(client, issue_code(client))
    assert response.headers["cache-control"] == "no-store"


def test_logout_kills_session(client: TestClient) -> None:
    csrf = open_session(client)
    assert client.delete("/web/session", headers=csrf_headers(csrf)).status_code == 200
    assert client.get("/web/api/state").status_code == 401


# --- сквозные сценарии -------------------------------------------------------


def test_phone_login_through_web(client: TestClient) -> None:
    csrf = open_session(client)
    created = client.post(
        "/web/api/accounts",
        json={"phone": "+79990000101", "label": "личный"},
        headers=csrf_headers(csrf),
    )
    assert created.status_code == 201
    account_id = created.json()["id"]

    started = client.post(
        "/web/api/login/start", json={"account_id": account_id}, headers=csrf_headers(csrf)
    )
    assert started.status_code == 200

    completed = client.post(
        "/web/api/login/complete",
        json={"challenge_id": started.json()["challenge_id"], "code": STUB_CODE},
        headers=csrf_headers(csrf),
    )
    assert completed.status_code == 200
    assert completed.json()["state"] == "ready"

    state = client.get("/web/api/state").json()
    assert state["status"]["accounts_ready"] == 1
    assert state["accounts"][0]["state"] == "ready"


def test_bad_code_is_reported(client: TestClient) -> None:
    csrf = open_session(client)
    account_id = client.post(
        "/web/api/accounts", json={"phone": "+79990000102"}, headers=csrf_headers(csrf)
    ).json()["id"]
    started = client.post(
        "/web/api/login/start", json={"account_id": account_id}, headers=csrf_headers(csrf)
    ).json()
    response = client.post(
        "/web/api/login/complete",
        json={"challenge_id": started["challenge_id"], "code": "11111"},
        headers=csrf_headers(csrf),
    )
    assert response.status_code == 400


def test_qr_login_through_web(client: TestClient) -> None:
    csrf = open_session(client)
    account_id = client.post(
        "/web/api/accounts", json={"phone": "+79990000103"}, headers=csrf_headers(csrf)
    ).json()["id"]

    started = client.post(
        "/web/api/login/qr/start", json={"account_id": account_id}, headers=csrf_headers(csrf)
    )
    assert started.status_code == 200
    body = started.json()
    # Код рисуется на сервере: странице не нужны сторонние скрипты.
    prefix = "data:image/svg+xml;base64,"
    assert body["image"].startswith(prefix)
    svg = base64.b64decode(body["image"][len(prefix) :]).decode("utf-8")
    assert svg.startswith("<svg") and "<rect" in svg

    pending = client.post(
        "/web/api/login/qr/poll",
        json={"challenge_id": body["challenge_id"]},
        headers=csrf_headers(csrf),
    )
    assert pending.json() == {"status": "pending", "account": None}

    service: UserbotService = client.app.state.service  # type: ignore[attr-defined]
    transport = service._connections.get(account_id)
    assert isinstance(transport, StubTransport)
    transport.confirm_qr(body["challenge_id"])

    confirmed = client.post(
        "/web/api/login/qr/poll",
        json={"challenge_id": body["challenge_id"]},
        headers=csrf_headers(csrf),
    ).json()
    assert confirmed["status"] == "confirmed"
    assert confirmed["account"]["state"] == "ready"


def test_disable_through_web(client: TestClient) -> None:
    csrf = open_session(client)
    account_id = client.post(
        "/web/api/accounts", json={"phone": "+79990000104"}, headers=csrf_headers(csrf)
    ).json()["id"]
    response = client.post(
        f"/web/api/accounts/{account_id}/disable",
        json={"reason": "проверка"},
        headers=csrf_headers(csrf),
    )
    assert response.status_code == 200
    assert response.json()["state"] == "disabled"
    assert response.json()["last_error"] == "проверка"
