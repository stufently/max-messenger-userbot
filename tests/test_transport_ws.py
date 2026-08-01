"""Адаптер PyMax против настоящего websocket-сервера.

Зачем отдельно от [test_transport_pymax][tests.test_transport_pymax]. Там
библиотека подменена модулем в `sys.modules`, и это правильно для того, что
проверяется там: раскладка ошибок и дисциплина курсора не требуют сокета. Но у
подмены есть цена — тесты остаются зелёными при любой версии `websockets`,
потому что до неё дело не доходит. А `websockets` — единственная зависимость,
которая сама говорит с сервером MAX: её поломка вылезла бы не в CI, а у
пользователя на живом соединении. Обновление до 17.0 из-за этого и было
отложено (причина записана в `packaging/constraints.txt`).

Здесь наоборот: библиотека настоящая, сокет настоящий, JSON по проводу
настоящий — выдуман только собеседник. Сервер отвечает ровно на то, что PyMax
спрашивает при web-входе по сохранённому токену: `SESSION_INIT`, `LOGIN`,
`PING`, `MSG_SEND`. Аккаунта MAX не требуется, наружу тест не ходит: адрес
задаётся явно и указывает на 127.0.0.1.

Именно «указывает явно» здесь не формальность. При отладке этого теста
незаданный адрес однажды увёл клиент на настоящий `wss://ws-api.oneme.ru`, и
поддельный токен ушёл в MAX. Поэтому адрес не подменяется обходным путём
(monkeypatch модуля, подмена умолчания поля) — он передаётся параметром, и если
параметр потеряется, тест не «тихо сходит в интернет», а не пройдёт handshake с
собственным сервером.

В матричных job-ах CI ставится только `.[dev]`, там теста нет и он честно
пропускается. Настоящий гейт — job с образом: внутри `dev,pymax`, и там же
отдельный шаг запускает этот файл, требуя, чтобы он именно прошёл, а не был
пропущен.
"""

from __future__ import annotations

import asyncio
import json
import socket
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest

pytest.importorskip("pymax", reason="транспорт pymax ставится отдельным extra")
serve = pytest.importorskip(
    "websockets.asyncio.server", reason="websockets приходит вместе с pymax"
).serve

from maxub.core.models import Session  # noqa: E402
from maxub.transport.base import TransportOutcomeUnknown  # noqa: E402
from maxub.transport.pymax import PyMaxTransport  # noqa: E402
from maxub.transport.pymax_client import build_extra_config  # noqa: E402
from maxub.transport.pymax_runtime import ClientRuntime  # noqa: E402
from maxub.transport.pymax_session import Envelope, decode, encode  # noqa: E402

# Опкоды внутреннего протокола MAX (`pymax.protocol.enums.Opcode`) и типы кадров
# (`Command`). Продублированы числами намеренно: сервер обязан говорить теми же
# значениями, что уходят по проводу, а не теми, которые библиотека сегодня
# держит в перечислении. Разъедься они — это и есть поломка, которую тест ловит.
SESSION_INIT, PING, LOGIN, MSG_SEND, NOTIF_MESSAGE = 6, 1, 19, 64, 128
REQUEST, RESPONSE = 0, 1

#: Все ожидания в тесте короткие: штатный `CONNECT_WAIT` адаптера — 60 секунд, и
#: молчащий сервер держал бы раннер минуту вместо внятного падения.
WAIT = 5.0

TOKEN = "токен-веб-входа"
DEVICE_ID = "устройство-под-тест"
CHAT_ID = 42
SELF_ID = 777
PEER_ID = 999


def _profile(user_id: int = SELF_ID) -> dict[str, Any]:
    return {"contact": {"id": user_id, "names": [{"name": "Я", "type": "TYPE_UNKNOWN"}]}}


def _message(
    message_id: int, *, text: str, sender: int, chat_id: int | None = CHAT_ID
) -> dict[str, Any]:
    """Сообщение в том виде, в каком его присылает MAX: camelCase, время в мс."""
    body: dict[str, Any] = {
        "id": message_id,
        "sender": sender,
        "text": text,
        "time": 1_700_000_000_000,
        "type": "TEXT",
    }
    if chat_id is not None:
        body["chatId"] = chat_id
    return body


class FakeMax:
    """Сервер MAX ровно в том объёме, который нужен web-входу по токену.

    Кадры разбираются по опкоду, а не по порядку: ping-loop PyMax стартует
    сразу после handshake и успевает вклиниться между ним и логином. Тест,
    ожидающий строгую последовательность, падал бы через раз.
    """

    def __init__(self) -> None:
        self.url = ""
        self.server: Any | None = None
        #: Всё, что пришло по проводу, — по нему тест и судит о handshake.
        self.inbox: list[dict[str, Any]] = []
        self.origins: list[str | None] = []
        #: Токен, который LOGIN вернёт вместо предъявленного (ротация на сервере).
        self.rotate_to: str | None = None
        self.event_delivered = asyncio.Event()

    # --- сторона сервера ----------------------------------------------------

    async def handle(self, ws: Any) -> None:
        self.origins.append(ws.request.headers.get("Origin"))
        async for raw in ws:
            frame = json.loads(raw)
            self.inbox.append(frame)
            opcode, seq = frame["opcode"], frame["seq"]
            if opcode == SESSION_INIT:
                await self._respond(ws, opcode, seq, {"time": 1})
            elif opcode == LOGIN:
                await self._respond(ws, opcode, seq, self._login_payload())
                # Событие уходит сразу за логином: до него клиент ещё не считает
                # себя запущенным и обработчик сообщений не подписан.
                await ws.send(
                    json.dumps(
                        {
                            "opcode": NOTIF_MESSAGE,
                            "cmd": REQUEST,
                            "seq": 0,
                            "payload": _message(11, text="привет", sender=PEER_ID),
                        }
                    )
                )
                self.event_delivered.set()
            elif opcode == PING:
                await self._respond(ws, opcode, seq, {})
            elif opcode == MSG_SEND:
                sent = frame["payload"]["message"]["text"]
                await self._respond(ws, opcode, seq, _message(12, text=sent, sender=SELF_ID))
            else:  # pragma: no cover — сервер обязан молчать, а не гадать
                raise AssertionError(f"неожиданный опкод {opcode}")

    def _login_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "profile": _profile(),
            "chats": [],
            "messages": {},
            "contacts": [],
        }
        if self.rotate_to is not None:
            payload["token"] = self.rotate_to
        return payload

    @staticmethod
    async def _respond(ws: Any, opcode: int, seq: int, payload: dict[str, Any]) -> None:
        await ws.send(
            json.dumps({"opcode": opcode, "cmd": RESPONSE, "seq": seq, "payload": payload})
        )

    async def hang_up(self) -> None:
        """Закрывает соединение со своей стороны — как это делает MAX."""
        assert self.server is not None
        for connection in list(self.server.connections):
            await connection.close()

    # --- разбор того, что пришло --------------------------------------------

    def first(self, opcode: int) -> dict[str, Any]:
        for frame in self.inbox:
            if frame["opcode"] == opcode:
                return frame
        raise AssertionError(f"опкод {opcode} по проводу не приходил: {self.opcodes()}")

    def opcodes(self) -> list[int]:
        return [frame["opcode"] for frame in self.inbox]


@pytest.fixture
async def max_server() -> AsyncIterator[FakeMax]:
    fake = FakeMax()
    # Порт выбирает ядро (`port=0`), и берётся он с уже открытого сокета: искать
    # свободный порт заранее — гонка с любым другим процессом на машине.
    async with serve(fake.handle, "127.0.0.1", 0) as server:
        fake.server = server
        port = next(iter(server.sockets)).getsockname()[1]
        fake.url = f"ws://127.0.0.1:{port}/websocket"
        yield fake


@pytest.fixture
def no_way_out(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Отменяет библиотечное умолчание адреса на время теста.

    Страховка от одной конкретной ошибки, а не от абстрактной. Пока этот тест
    отлаживался, адрес однажды не доехал до клиента — и тот молча ушёл на
    настоящий `wss://ws-api.oneme.ru`, унеся туда поддельный токен. Проверки
    поймали бы это потом, а запрос ушёл бы уже сейчас, и в CI, где сеть есть,
    он уходил бы при каждом прогоне.

    Поэтому умолчание подменяется заведомо закрытым адресом: потеряй проброс
    `endpoint` — тест упрётся в свой же localhost и покраснеет, а наружу не
    пойдёт. Порт для этого берётся не наугад: сокет открывается и занимает
    порт, но не слушает его, поэтому подключение туда гарантированно
    отвергается. Постоянный номер (вроде первого порта) такой гарантии не даёт
    — его может слушать что угодно на машине, и тест вместо внятного отказа
    постучался бы в постороннюю службу.

    Проверку самого умолчания это не отменяет, она живёт отдельно и этой
    подмены не видит.
    """
    import pymax

    original = pymax.ExtraConfig

    with socket.socket() as closed:
        closed.bind(("127.0.0.1", 0))
        dead_end = f"ws://127.0.0.1:{closed.getsockname()[1]}/websocket"

        def guarded(**kwargs: Any) -> Any:
            kwargs.setdefault("url", dead_end)
            return original(**kwargs)

        monkeypatch.setattr(pymax, "ExtraConfig", guarded)
        yield


@pytest.fixture
async def transport(max_server: FakeMax, no_way_out: None) -> AsyncIterator[PyMaxTransport]:
    connected = PyMaxTransport(endpoint=max_server.url, request_timeout=WAIT)
    try:
        yield connected
    finally:
        # Уборка обязательна и после падения: живой клиент держит соединение и
        # фоновую задачу, а следующий тест поднимет свой сервер на своём порту.
        await connected.disconnect()


def web_session(token: str = TOKEN) -> Session:
    """Сессия web-входа: только с ней адаптер берёт websocket, а не TCP."""
    return Session(
        account_id=1,
        phone="+79990000000",
        token=encode(Envelope(kind="web", token=token, mt_instance_id="", user_agent=None)),
        device_id=DEVICE_ID,
    )


# --- сам провод -------------------------------------------------------------


async def test_handshake_and_login_really_go_over_the_socket(
    transport: PyMaxTransport, max_server: FakeMax
) -> None:
    """Подключение — это не «сокет открылся», а разговор по протоколу.

    Проверяется то, что видел сервер: версия протокола, тип кадра, подпись
    web-устройства, наш `device_id` и предъявленный токен. Без этого тест
    зеленел бы и на клиенте, который открыл соединение и замолчал.
    """
    async with asyncio.timeout(WAIT):
        assert await transport.connect(web_session()) is None

    handshake = max_server.first(SESSION_INIT)
    assert handshake["ver"] == 11
    assert handshake["cmd"] == REQUEST
    assert handshake["payload"]["userAgent"]["deviceType"] == "WEB"
    assert handshake["payload"]["deviceId"] == DEVICE_ID

    assert max_server.first(LOGIN)["payload"]["token"] == TOKEN
    # Origin MAX проверяет на стороне сервера; подставляет его библиотека.
    assert max_server.origins == ["https://web.max.ru"]
    # Ответы разбираются по `seq`, поэтому повтор номера склеил бы два разных
    # запроса в один ответ.
    seqs = [frame["seq"] for frame in max_server.inbox]
    assert len(seqs) == len(set(seqs))


async def test_incoming_message_reaches_the_core(
    transport: PyMaxTransport, max_server: FakeMax
) -> None:
    async with asyncio.timeout(WAIT):
        await transport.connect(web_session())
        await max_server.event_delivered.wait()
        update = await anext(transport.events())

    assert update.message.remote_id == "11"
    assert update.message.chat_id == str(CHAT_ID)
    assert update.message.text == "привет"
    assert update.message.outgoing is False
    # Серверной позиции в потоке PyMax не сообщает — курсор обязан остаться пустым.
    assert update.cursor is None


async def test_outgoing_message_travels_the_same_socket(
    transport: PyMaxTransport, max_server: FakeMax
) -> None:
    """Приём и отправка ломаются по-разному: `recv` и `send` — разные пути."""
    async with asyncio.timeout(WAIT):
        await transport.connect(web_session())
        remote_id = await transport.send_text(str(CHAT_ID), "ответ", "client-token")

    assert remote_id == "12"
    sent = max_server.first(MSG_SEND)["payload"]
    assert sent["chatId"] == CHAT_ID
    assert sent["message"]["text"] == "ответ"


async def test_server_close_is_reported_as_a_break(
    transport: PyMaxTransport, max_server: FakeMax
) -> None:
    """Закрытие сервером доходит до ядра ошибкой, а не тихим концом потока.

    Это и есть та часть, ради которой нужен настоящий сокет. В коде адаптера
    предусмотрены оба исхода — `events()` умеет закончиться молча, — но живая
    библиотека всегда превращает закрытие в ошибку соединения: PyMax помечает
    её `_connection_lost`, `ClientRuntime` сохраняет, и следующий шаг потока
    поднимает `TransportOutcomeUnknown`. Ровно это поведение уже закреплено на
    подменной библиотеке в `test_broken_connection_ends_stream`; здесь оно
    подтверждается на настоящем закрытии — с кодом 1000, то есть штатным.
    """
    async with asyncio.timeout(WAIT):
        await transport.connect(web_session())
        stream = transport.events()
        await max_server.event_delivered.wait()
        assert (await anext(stream)).message.remote_id == "11"

        await max_server.hang_up()
        with pytest.raises(TransportOutcomeUnknown):
            await anext(stream)


async def test_rotated_token_comes_back_in_the_session(
    transport: PyMaxTransport, max_server: FakeMax
) -> None:
    """Новый токен от сервера обязан вернуться ядру, иначе он умрёт в памяти.

    Цепочка длинная и вся на стороне библиотеки: LOGIN отвечает другим токеном,
    PyMax сообщает его хранилищу через `update_token`, а адаптер сверяет
    сохранённое с предъявленным. На подменной библиотеке это проверяет наш же
    фейк; здесь — настоящий разбор ответа настоящей библиотекой.
    """
    max_server.rotate_to = "выданный-заново"

    async with asyncio.timeout(WAIT):
        refreshed = await transport.connect(web_session())

    assert refreshed is not None
    assert decode(refreshed.token).token == "выданный-заново"
    # Остальное в сессии не должно поехать: меняется только токен.
    assert refreshed.device_id == DEVICE_ID
    assert decode(refreshed.token).kind == "web"


# --- сам параметр адреса ------------------------------------------------------


def test_endpoint_defaults_to_the_library_address() -> None:
    """Без явного адреса умолчание остаётся за PyMax, а не дублируется у нас.

    Своя копия адреса разошлась бы с библиотечной при первом же обновлении — и
    разошлась бы молча, потому что проверять её было бы нечем.

    Сравнить итоговые строки мало: скопированное сегодня умолчание совпадает с
    библиотечным ровно до того обновления, ради которого проверка и заведена.
    Поэтому проверяется не значение, а факт присваивания — pydantic помечает
    поле как заданное при любом присваивании, включая присваивание того же
    самого значения.
    """
    import pymax

    runtime = ClientRuntime()
    default = build_extra_config(pymax, runtime, web=True, proxy=None, request_timeout=WAIT)
    assert "url" not in default.model_fields_set
    assert default.url == pymax.ExtraConfig().url

    overridden = build_extra_config(
        pymax,
        ClientRuntime(),
        web=True,
        proxy=None,
        request_timeout=WAIT,
        endpoint="ws://127.0.0.1:9/websocket",
    )
    assert "url" in overridden.model_fields_set
    assert overridden.url == "ws://127.0.0.1:9/websocket"
