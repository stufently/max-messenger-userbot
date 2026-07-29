"""Тесты шины событий и живого потока `WS /ws/events`.

Шина — не источник правды: правда в журнале событий, а подписка лишь ускоряет
доставку. Отсюда её главное свойство, которое здесь и проверяется: медленный
подписчик теряет события, но не может ни раздуть память демона, ни притормозить
тех, кто их публикует. Клиенту, которому нужна полнота, остаётся журнал и
курсор `after_id` — этот путь проверяется в `test_api.py`.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from maxub.api.app import create_app
from maxub.config import Settings
from maxub.core.events import EventBus, account_state_event
from maxub.core.models import AccountState, Event
from maxub.transport.stub import STUB_CODE


def _event(kind: str = "message.received", dedup_key: str = "k1") -> Event:
    return Event(account_id=1, kind=kind, dedup_key=dedup_key)


def test_publish_reaches_every_subscriber() -> None:
    bus = EventBus()
    first, second = bus.subscribe(), bus.subscribe()

    bus.publish(_event())

    assert first.qsize() == 1
    assert second.qsize() == 1


def test_publish_without_subscribers_is_not_an_error() -> None:
    """Обычное состояние демона: панель закрыта, событий это не отменяет."""
    EventBus().publish(_event())


def test_unsubscribe_stops_delivery() -> None:
    bus = EventBus()
    queue = bus.subscribe()
    bus.unsubscribe(queue)

    bus.publish(_event())

    assert queue.empty()


def test_unsubscribe_twice_is_harmless() -> None:
    """Отписка идёт из `finally` разорванного соединения и может повториться."""
    bus = EventBus()
    queue = bus.subscribe()

    bus.unsubscribe(queue)
    bus.unsubscribe(queue)


def test_slow_subscriber_loses_events_instead_of_growing_memory() -> None:
    """Переполнение — это потеря события, а не рост очереди без предела.

    Осознанный размен: демон живёт неделями, а вкладка панели может «уснуть» в
    свёрнутом браузере. Расплата за неограниченную очередь — память процесса,
    расплата за потерю — один дочитанный запрос по курсору.
    """
    bus = EventBus(queue_size=2)
    queue = bus.subscribe()

    for number in range(5):
        bus.publish(_event(dedup_key=f"k{number}"))

    assert queue.qsize() == 2


def test_slow_subscriber_does_not_block_a_fast_one() -> None:
    """Забитая очередь одного подписчика не мешает остальным получить событие."""
    bus = EventBus(queue_size=1)
    slow, fast = bus.subscribe(), bus.subscribe()
    bus.publish(_event())
    fast.get_nowait()

    bus.publish(_event(kind="message.sent", dedup_key="k2"))

    assert fast.qsize() == 1
    assert slow.qsize() == 1


async def test_subscriber_waits_for_the_next_event() -> None:
    """Подписка — это ожидание, а не опрос: событие приходит само."""
    bus = EventBus()
    queue = bus.subscribe()
    waiting = asyncio.ensure_future(queue.get())
    await asyncio.sleep(0)

    bus.publish(_event(kind="account.ready"))
    received = await asyncio.wait_for(waiting, timeout=1.0)

    assert received.kind == "account.ready"


@pytest.fixture
def app_client(tmp_path: Path) -> Iterator[TestClient]:
    settings = Settings(
        data_dir=tmp_path,
        transport="stub",
        send_rate_per_minute=6000.0,
        send_burst=50,
        send_jitter_seconds=0.0,
    )
    app = create_app(settings)
    with TestClient(app) as client:
        client.headers.update({"Authorization": f"Bearer {app.state.api_token}"})
        yield client


def test_ws_rejects_a_missing_token(app_client: TestClient) -> None:
    """Живой поток отдаёт события аккаунтов — без токена его быть не должно."""
    with pytest.raises(WebSocketDisconnect) as refused:
        with app_client.websocket_connect("/ws/events", headers={"Authorization": ""}):
            pass

    assert refused.value.code == 4401


def test_ws_rejects_a_wrong_token(app_client: TestClient) -> None:
    with pytest.raises(WebSocketDisconnect):
        with app_client.websocket_connect("/ws/events", headers={"Authorization": "Bearer wrong"}):
            pass


def test_ws_delivers_events_as_they_happen(app_client: TestClient) -> None:
    """Событие доходит до открытого сокета без опроса журнала."""
    with app_client.websocket_connect("/ws/events") as socket:
        account_id = app_client.post("/accounts", json={"phone": "+79990000101"}).json()["id"]
        challenge_id = app_client.post("/login/start", json={"account_id": account_id}).json()[
            "challenge_id"
        ]
        app_client.post("/login/complete", json={"challenge_id": challenge_id, "code": STUB_CODE})
        # Событие сначала попадает в журнал и только потом раздаётся
        # подписчикам, поэтому его наличие в журнале означает, что читать сокет
        # уже есть что. Без этой проверки провал выглядел бы как зависший тест:
        # у `receive_text` нет таймаута.
        assert app_client.get("/events").json()

        payload = json.loads(socket.receive_text())

    assert payload["kind"] == "account.ready"
    assert payload["account_id"] == account_id


def test_state_events_cover_what_needs_a_human() -> None:
    """О потере авторизации, обрыве и выключении подписчик узнаёт из потока."""
    for state in (AccountState.AUTH_REQUIRED, AccountState.BACKOFF, AccountState.DISABLED):
        event = account_state_event(1, state, "причина")
        assert event is not None
        assert event.kind == f"account.{state.value}"
        assert event.payload == {"state": state.value, "error": "причина"}


def test_transient_states_are_not_announced() -> None:
    """`connecting` и `syncing` сменяются парами при каждом переподключении.

    В журнале они дали бы шум, за которым потерялось бы то, что действительно
    требует внимания.
    """
    assert account_state_event(1, AccountState.CONNECTING, None) is None
    assert account_state_event(1, AccountState.SYNCING, None) is None


def test_repeated_transitions_are_not_deduplicated() -> None:
    """Аккаунт может уходить в backoff много раз, и каждый раз это событие."""
    first = account_state_event(1, AccountState.BACKOFF, "обрыв")
    second = account_state_event(1, AccountState.BACKOFF, "обрыв")

    assert first is not None and second is not None
    assert first.dedup_key != second.dedup_key


def test_auth_loss_reaches_the_journal(app_client: TestClient) -> None:
    """Сквозная проверка: выключенный аккаунт оставляет след в журнале."""
    account_id = app_client.post("/accounts", json={"phone": "+79990000103"}).json()["id"]

    app_client.post(f"/accounts/{account_id}/disable", json={"reason": "проверка"})

    kinds = [event["kind"] for event in app_client.get("/events").json()]
    assert "account.disabled" in kinds
