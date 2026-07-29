"""Тесты адаптера PyMax.

Живого аккаунта MAX нет и не будет, поэтому библиотека подменяется модулем в
`sys.modules`. Подмена повторяет ровно то, на что адаптер опирается: порядок
вызовов при входе, форму ответов и набор исключений. В сеть тесты не ходят.

Проверяется не «работает ли отправка», а то, что дороже: честность
`Capabilities`, раскладка ошибок и дисциплина курсора — места, где ошибка стоит
дубля у получателя или потерянного сообщения.
"""

from __future__ import annotations

import asyncio
import sys
import types
from typing import Any

import pytest
from pydantic import BaseModel

from maxub.transport import available, get_factory
from maxub.transport.base import (
    ReconcileOutcome,
    TransportAuthError,
    TransportError,
    TransportNotApplied,
    TransportOutcomeUnknown,
    TransportPermanent,
    TransportRateLimited,
    TransportUnsupported,
)
from maxub.transport.pymax_errors import translate
from maxub.transport.pymax_session import decode

# --- подменная библиотека ---------------------------------------------------


class FakePyMaxError(Exception):
    pass


class FakeUploadError(FakePyMaxError):
    pass


class FakeApiError(FakePyMaxError):
    def __init__(
        self,
        *,
        opcode: int = 0,
        error: str | None = None,
        message: str | None = None,
        localized_message: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        self.opcode = opcode
        self.error = error
        self.message = message
        self.localized_message = localized_message
        self.payload = payload or {}
        super().__init__(error or message or "api error")


class FakeUserAgent(BaseModel):
    device_type: str = "ANDROID"
    device_name: str = "Pixel 7"


class FakeSessionInfo(BaseModel):
    token: str
    device_id: str
    phone: str = ""
    mt_instance_id: str = ""


class FakeMessage:
    def __init__(
        self,
        message_id: int,
        chat_id: int | None,
        sender: int | None = None,
        text: str = "",
        time: int = 1_700_000_000_000,
    ) -> None:
        self.id = message_id
        self.chat_id = chat_id
        self.sender = sender
        self.text = text
        self.time = time


class FakeExtraConfig:
    def __init__(self, **kwargs: Any) -> None:
        self.__dict__.update(kwargs)
        self.mt_instance_id = "mt-generated"
        self.user_agent: Any = None

    def generate_user_agent(self) -> FakeUserAgent:
        return FakeUserAgent()

    def generate_web_user_agent(self) -> FakeUserAgent:
        return FakeUserAgent(device_type="WEB", device_name="Chrome")


class FakeServer:
    """Состояние подменного MAX: чем он отвечает и когда ломается."""

    def __init__(self) -> None:
        self.self_id = 500
        self.valid_code = "1234"
        self.login_token = "login-token"
        self.qr_link = "https://max.ru/qr/abc"
        self.qr_token = "qr-token"
        self.qr_confirmed = asyncio.Event()
        self.qr_error: BaseException | None = None
        self.login_error: BaseException | None = None
        self.send_error: BaseException | None = None
        self.send_returns_none = False
        self.history: list[FakeMessage] = []
        self.clients: list[FakeClient] = []
        self._next_id = 1000

    def next_id(self) -> int:
        self._next_id += 1
        return self._next_id

    def check_code(self, code: str) -> Any:
        if code != self.valid_code:
            return types.SimpleNamespace(
                login_token=None, password_challenge=None, register_token=None
            )
        return types.SimpleNamespace(
            login_token=self.login_token, password_challenge=None, register_token=None
        )

    def drop(self, error: BaseException | None) -> None:
        """Имитирует обрыв соединения у последнего поднятого клиента."""
        client = self.clients[-1]
        client.stream_error = error
        client.closed.set()


class FakeAuthApi:
    def __init__(self, server: FakeServer) -> None:
        self._server = server

    async def request_code(self, phone: str) -> Any:
        return types.SimpleNamespace(token="sms-request-token")

    async def send_code(self, token: str, code: str) -> Any:
        return self._server.check_code(code)


class FakeClient:
    server: FakeServer
    web = False

    def __init__(
        self,
        phone: str | None = None,
        session_name: str = "session.db",
        work_dir: str = ".",
        extra_config: Any = None,
        auth_flow: Any = None,
        **_: Any,
    ) -> None:
        self.phone = phone
        self.extra_config = extra_config
        self.auth_flow = auth_flow
        self.me = types.SimpleNamespace(contact=types.SimpleNamespace(id=self.server.self_id))
        self.closed = asyncio.Event()
        self.stream_error: BaseException | None = None
        self._closing = False
        self._on_start: list[Any] = []
        self._on_message: list[Any] = []
        self.server.clients.append(self)

    def on_start(self) -> Any:
        def register(callback: Any) -> Any:
            self._on_start.append(callback)
            return callback

        return register

    def on_message(self, *filters: Any) -> Any:
        def register(callback: Any) -> Any:
            self._on_message.append(callback)
            return callback

        return register

    async def start(self) -> None:
        token = getattr(self.extra_config, "token", None)
        if token is None:
            app = types.SimpleNamespace(
                config=types.SimpleNamespace(phone=self.phone),
                api=types.SimpleNamespace(auth=FakeAuthApi(self.server)),
            )
            result = await self.auth_flow.authenticate(app)
            token = result.token
            if not token:
                raise RuntimeError("Authentication failed: no token received")
        if self.server.login_error is not None:
            raise self.server.login_error
        await self.extra_config.store.save_session(
            FakeSessionInfo(
                token=token,
                device_id=getattr(self.extra_config, "device_id", None) or "device-1",
                phone=self.phone or "",
                mt_instance_id=self.extra_config.mt_instance_id,
            )
        )
        for callback in self._on_start:
            await callback(self)
        await self.closed.wait()
        if self.stream_error is not None and not self._closing:
            raise self.stream_error

    async def close(self) -> None:
        self._closing = True
        self.closed.set()

    async def deliver(self, message: FakeMessage) -> None:
        for callback in self._on_message:
            await callback(message, self)

    async def send_message(self, chat_id: int, text: str, *args: Any, **kwargs: Any) -> Any:
        if self.server.send_error is not None:
            error, self.server.send_error = self.server.send_error, None
            raise error
        if self.server.send_returns_none:
            return None
        return FakeMessage(self.server.next_id(), chat_id, self.server.self_id, text)

    async def fetch_history(self, chat_id: int, backward: int = 40, **kwargs: Any) -> Any:
        return list(self.server.history)


class FakeWebClient(FakeClient):
    web = True


class FakeQrAuthFlow:
    server: FakeServer

    def __init__(self, qr_provider: Any, password_provider: Any = None) -> None:
        self._qr = qr_provider

    async def authenticate(self, app: Any) -> Any:
        await self._qr.show_qr(self.server.qr_link)
        await self.server.qr_confirmed.wait()
        if self.server.qr_error is not None:
            raise self.server.qr_error
        return types.SimpleNamespace(token=self.server.qr_token)


def install_fake_pymax(monkeypatch: pytest.MonkeyPatch) -> FakeServer:
    """Ставит подменный ``pymax`` в `sys.modules` на время теста."""
    server = FakeServer()
    module = types.ModuleType("pymax")
    module.Client = type("Client", (FakeClient,), {"server": server})  # type: ignore[attr-defined]
    module.WebClient = type("WebClient", (FakeWebClient,), {"server": server})  # type: ignore[attr-defined]
    module.QrAuthFlow = type("QrAuthFlow", (FakeQrAuthFlow,), {"server": server})  # type: ignore[attr-defined]
    module.ExtraConfig = FakeExtraConfig  # type: ignore[attr-defined]
    module.ApiError = FakeApiError  # type: ignore[attr-defined]
    module.PyMaxError = FakePyMaxError  # type: ignore[attr-defined]
    module.UploadError = FakeUploadError  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pymax", module)
    return server


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> FakeServer:
    return install_fake_pymax(monkeypatch)


@pytest.fixture
async def transport(server: FakeServer) -> Any:
    instance = get_factory("pymax")()
    try:
        yield instance
    finally:
        await instance.disconnect()


async def logged_in(transport: Any, server: FakeServer) -> Any:
    challenge = await transport.start_login("+79990000000")
    return await transport.complete_login(challenge.challenge_id, server.valid_code, 1)


# --- отсутствие библиотеки --------------------------------------------------


def test_missing_library_gives_readable_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Без PyMax выбор транспорта объясняет, чего не хватает."""
    monkeypatch.setitem(sys.modules, "pymax", None)
    with pytest.raises(TransportError) as info:
        get_factory("pymax")()
    assert "max-userbot[pymax]" in str(info.value)


def test_stub_survives_missing_library(monkeypatch: pytest.MonkeyPatch) -> None:
    """Реестр не разваливается: заглушка доступна и без PyMax."""
    monkeypatch.setitem(sys.modules, "pymax", None)
    assert "pymax" in available()
    assert get_factory("stub")().name == "stub"


# --- таксономия ошибок ------------------------------------------------------


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (FakeApiError(error="flood.wait"), TransportRateLimited),
        (FakeApiError(error="user.not.authorized"), TransportAuthError),
        (FakeApiError(error="chat.not.found"), TransportPermanent),
        (FakeApiError(error="server.internal.hiccup"), TransportOutcomeUnknown),
        (FakePyMaxError("Invalid response payload"), TransportOutcomeUnknown),
        (FakeUploadError("upload failed"), TransportOutcomeUnknown),
        (TimeoutError("нет ответа"), TransportOutcomeUnknown),
        (ConnectionError("сокет закрыт"), TransportOutcomeUnknown),
        (OSError("сеть недоступна"), TransportOutcomeUnknown),
        (ValueError("что-то своё"), TransportOutcomeUnknown),
    ],
)
def test_error_taxonomy(
    server: FakeServer, error: BaseException, expected: type[TransportError]
) -> None:
    assert isinstance(translate(error), expected)


def test_unknown_api_error_is_not_treated_as_not_applied(server: FakeServer) -> None:
    """Отказ без опознанного кода не даёт права на повтор.

    `ApiError` доказывает только то, что ответ пришёл. Считать его «точно не
    выполнено» значило бы разрешить ядру повтор — и получить дубль.
    """
    result = translate(FakeApiError(error="unknown.reason"))
    assert isinstance(result, TransportOutcomeUnknown)
    assert not isinstance(result, TransportNotApplied)


def test_rate_limit_carries_retry_after(server: FakeServer) -> None:
    error = FakeApiError(error="too.many.requests", payload={"retryAfter": 12})
    result = translate(error)
    assert isinstance(result, TransportRateLimited)
    assert result.retry_after == 12
    # Значение больше секунды сервер присылает в миллисекундах.
    milliseconds = translate(FakeApiError(error="flood", payload={"retryAfter": 4500}))
    assert isinstance(milliseconds, TransportRateLimited)
    assert milliseconds.retry_after == 4.5


def test_api_error_during_auth_is_auth_error(server: FakeServer) -> None:
    result = translate(FakeApiError(error="server.internal.hiccup"), during_auth=True)
    assert isinstance(result, TransportAuthError)


def test_transport_errors_pass_through(server: FakeServer) -> None:
    original = TransportAuthError("уже разложено")
    assert translate(original) is original


# --- честность Capabilities -------------------------------------------------


async def test_backfill_off_means_fetch_updates_refuses(transport: Any) -> None:
    assert transport.capabilities.backfill is False
    with pytest.raises(TransportUnsupported):
        await transport.fetch_updates(None, 10)


async def test_reconcile_off_never_claims_absence(transport: Any, server: FakeServer) -> None:
    """Сверка не отвечает `NOT_FOUND`, потому что доказать отсутствие нечем."""
    assert transport.capabilities.reconcile is False
    session = await logged_in(transport, server)
    await transport.connect(session)
    await transport.send_text("42", "привет", "token-1")
    result = await transport.reconcile_send("42", "token-1")
    assert result.outcome is ReconcileOutcome.INCONCLUSIVE
    # И для заведомо не отправленного тоже: отсутствие остаётся недоказуемым.
    missing = await transport.reconcile_send("42", "никогда-не-отправлялся")
    assert missing.outcome is ReconcileOutcome.INCONCLUSIVE


def test_capabilities_do_not_promise_absent_methods(transport: Any) -> None:
    """Правка, удаление и вложения выключены: в контракте таких вызовов нет."""
    assert transport.capabilities.edit_message is False
    assert transport.capabilities.delete_message is False
    assert transport.capabilities.media is False
    assert not hasattr(transport, "edit_message")
    assert not hasattr(transport, "delete_message")


# --- вход -------------------------------------------------------------------


async def test_phone_login_returns_session_envelope(transport: Any, server: FakeServer) -> None:
    session = await logged_in(transport, server)
    envelope = decode(session.token)
    assert envelope.kind == "tcp"
    assert envelope.token == server.login_token
    assert session.device_id == "device-1"
    # Отпечаток устройства сохраняется, иначе каждое переподключение выглядело
    # бы для MAX как новый телефон.
    assert envelope.user_agent is not None
    assert envelope.user_agent["device_type"] == "ANDROID"


async def test_wrong_code_keeps_login_alive(transport: Any, server: FakeServer) -> None:
    """Неверный код — повод повторить ввод, а не начинать вход заново."""
    challenge = await transport.start_login("+79990000000")
    with pytest.raises(TransportAuthError):
        await transport.complete_login(challenge.challenge_id, "0000", 1)
    session = await transport.complete_login(challenge.challenge_id, server.valid_code, 1)
    assert decode(session.token).token == server.login_token


async def test_unknown_challenge_is_refused(transport: Any, server: FakeServer) -> None:
    await transport.start_login("+79990000000")
    with pytest.raises(TransportAuthError):
        await transport.complete_login("нет-такого", server.valid_code, 1)


async def test_qr_login_waits_for_confirmation(transport: Any, server: FakeServer) -> None:
    challenge = await transport.start_qr_login()
    assert challenge.payload == server.qr_link
    assert await transport.poll_qr_login(challenge.challenge_id, 1) is None
    server.qr_confirmed.set()
    session = await _poll_until_ready(transport, challenge.challenge_id)
    envelope = decode(session.token)
    assert envelope.kind == "web"
    assert envelope.token == server.qr_token
    assert envelope.user_agent is not None
    assert envelope.user_agent["device_type"] == "WEB"


async def test_expired_qr_is_auth_error(transport: Any, server: FakeServer) -> None:
    challenge = await transport.start_qr_login()
    server.qr_error = RuntimeError("QR authentication expired")
    server.qr_confirmed.set()
    with pytest.raises(TransportAuthError):
        await _poll_until_ready(transport, challenge.challenge_id)


async def _poll_until_ready(transport: Any, challenge_id: str) -> Any:
    for _ in range(50):
        session = await transport.poll_qr_login(challenge_id, 1)
        if session is not None:
            return session
        await asyncio.sleep(0)
    raise AssertionError("вход по QR так и не завершился")


# --- соединение и отправка --------------------------------------------------


async def test_connect_restores_saved_device(transport: Any, server: FakeServer) -> None:
    session = await logged_in(transport, server)
    await transport.connect(session)
    extra = server.clients[-1].extra_config
    assert extra.token == server.login_token
    assert extra.device_id == session.device_id
    assert extra.user_agent.device_name == "Pixel 7"


async def test_connect_refuses_foreign_session(transport: Any, server: FakeServer) -> None:
    from maxub.core.models import Session

    foreign = Session(account_id=1, phone="+7", token="stub-abcdef", device_id="d")
    with pytest.raises(TransportAuthError):
        await transport.connect(foreign)


async def test_send_text_returns_remote_id(transport: Any, server: FakeServer) -> None:
    await transport.connect(await logged_in(transport, server))
    remote_id = await transport.send_text("42", "привет", "token-1")
    assert remote_id.isdigit()


async def test_send_without_connection_is_not_applied(transport: Any, server: FakeServer) -> None:
    """До сети запрос не дошёл — это единственный доказуемый случай повтора."""
    with pytest.raises(TransportNotApplied):
        await transport.send_text("42", "привет", "token-1")


async def test_send_failure_maps_to_taxonomy(transport: Any, server: FakeServer) -> None:
    await transport.connect(await logged_in(transport, server))
    server.send_error = FakeApiError(error="flood.wait", payload={"retryAfter": 3})
    with pytest.raises(TransportRateLimited):
        await transport.send_text("42", "привет", "token-1")
    server.send_error = TimeoutError("нет ответа")
    with pytest.raises(TransportOutcomeUnknown):
        await transport.send_text("42", "привет", "token-2")


async def test_send_without_answer_is_unknown(transport: Any, server: FakeServer) -> None:
    """Ответ без сообщения — не отказ: оно могло уйти получателю."""
    await transport.connect(await logged_in(transport, server))
    server.send_returns_none = True
    with pytest.raises(TransportOutcomeUnknown):
        await transport.send_text("42", "привет", "token-1")


async def test_non_numeric_chat_is_permanent(transport: Any, server: FakeServer) -> None:
    await transport.connect(await logged_in(transport, server))
    with pytest.raises(TransportPermanent):
        await transport.send_text("не-число", "привет", "token-1")


# --- события и курсор -------------------------------------------------------


async def test_events_report_no_position(transport: Any, server: FakeServer) -> None:
    """Живой поток не выдумывает позицию: серверной метки у PyMax нет.

    Подстановка сюда идентификатора сообщения увела бы курсор ядра в чужое
    пространство значений — ровно та ошибка, ради которой заведён `cursor`.
    """
    await transport.connect(await logged_in(transport, server))
    await server.clients[-1].deliver(FakeMessage(7, 42, sender=99, text="привет"))
    update = await anext(transport.events())
    assert update.cursor is None
    assert update.message.remote_id == "7"
    assert update.message.chat_id == "42"
    assert update.message.outgoing is False


async def test_cursor_spaces_agree(transport: Any, server: FakeServer) -> None:
    """Обе стороны контракта живут в одном пространстве: позиции нет нигде."""
    await transport.connect(await logged_in(transport, server))
    with pytest.raises(TransportUnsupported):
        await transport.fetch_updates(None, 10)
    await server.clients[-1].deliver(FakeMessage(7, 42, sender=99))
    assert (await anext(transport.events())).cursor is None


async def test_own_message_is_outgoing(transport: Any, server: FakeServer) -> None:
    await transport.connect(await logged_in(transport, server))
    await server.clients[-1].deliver(FakeMessage(8, 42, sender=server.self_id, text="я"))
    assert (await anext(transport.events())).message.outgoing is True


async def test_event_without_chat_is_skipped(transport: Any, server: FakeServer) -> None:
    """Сообщение без чата ядру некуда положить, поэтому оно не публикуется."""
    await transport.connect(await logged_in(transport, server))
    client = server.clients[-1]
    await client.deliver(FakeMessage(9, None, sender=99))
    await client.deliver(FakeMessage(10, 42, sender=99))
    assert (await anext(transport.events())).message.remote_id == "10"


async def test_broken_connection_ends_stream(transport: Any, server: FakeServer) -> None:
    """Обрыв обязан дойти до ядра: иначе надзор не узнает о нём никогда."""
    await transport.connect(await logged_in(transport, server))
    stream = transport.events()
    first = asyncio.ensure_future(anext(stream))
    await asyncio.sleep(0)
    server.drop(ConnectionError("сокет закрыт"))
    with pytest.raises(TransportOutcomeUnknown):
        await first


# --- история ----------------------------------------------------------------


async def test_history_is_chronological(transport: Any, server: FakeServer) -> None:
    await transport.connect(await logged_in(transport, server))
    server.history = [
        FakeMessage(3, 42, sender=99, text="третье", time=3000),
        FakeMessage(1, 42, sender=99, text="первое", time=1000),
        FakeMessage(2, None, sender=99, text="без чата", time=2000),
    ]
    messages = await transport.fetch_history("42", 10)
    assert [m.remote_id for m in messages] == ["1", "3"]


# --- живучесть --------------------------------------------------------------


async def test_cancelled_reader_keeps_event(transport: Any, server: FakeServer) -> None:
    """Снятие читателя не съедает уже вынутое событие.

    Отмена приходит ровно тогда, когда `Queue.get` уже отдал сообщение: если не
    вернуть его назад, оно исчезнет молча — ни в журнале, ни в событиях ядра
    его не будет.
    """
    await transport.connect(await logged_in(transport, server))
    stream = transport.events()
    first = asyncio.ensure_future(anext(stream))
    await asyncio.sleep(0)
    await server.clients[-1].deliver(FakeMessage(11, 42, sender=99, text="важное"))
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert (await anext(transport.events())).message.remote_id == "11"


async def test_rejected_session_closes_client(transport: Any, server: FakeServer) -> None:
    """Отвергнутая сессия не оставляет за собой открытое соединение.

    Клиент к этому моменту уже поднят и держит сокет: без уборки он дожил бы до
    перезапуска демона, хотя транспорт считает себя незанятым.
    """
    session = await logged_in(transport, server)
    server.login_error = FakeApiError(error="login.token.invalid")
    with pytest.raises(TransportAuthError):
        await transport.connect(session)
    assert server.clients[-1].closed.is_set()
    with pytest.raises(TransportNotApplied):
        await transport.send_text("42", "привет", "token-1")


async def test_expired_qr_closes_client(transport: Any, server: FakeServer) -> None:
    challenge = await transport.start_qr_login()
    server.qr_error = RuntimeError("QR authentication expired")
    server.qr_confirmed.set()
    with pytest.raises(TransportAuthError):
        await _poll_until_ready(transport, challenge.challenge_id)
    assert server.clients[-1].closed.is_set()
