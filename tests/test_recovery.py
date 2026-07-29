"""Тесты восстановления после сбоя, дедупликации и прав доступа.

Сценарии из ревью: падение между отправкой и записью результата, повторный
захват очереди двумя воркерами, входящие события после переподключения,
позиция в потоке и надзор за аккаунтом, который не удалось поднять.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from datetime import timedelta
from pathlib import Path

import pytest

from maxub.config import Settings
from maxub.core import service as service_module
from maxub.core import supervisor as supervisor_module
from maxub.core.backfill import BackfillStalled
from maxub.core.crypto import SecretBox
from maxub.core.models import AccountState, Message, OutboxState, Session
from maxub.core.sender import backoff_delay as real_backoff_delay
from maxub.core.service import UserbotService
from maxub.core.storage import Storage
from maxub.transport.base import TransportOutcomeUnknown, Update
from maxub.transport.stub import STUB_CODE, StubTransport


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path,
        transport="stub",
        send_rate_per_minute=6000.0,
        send_burst=50,
        send_jitter_seconds=0.0,
        # Переподключение проверяется на порядок величин, а не на реальные
        # паузы: тест не должен спать секундами.
        reconnect_base_seconds=0.01,
        reconnect_max_seconds=0.05,
    )


def _new_service(settings: Settings) -> UserbotService:
    return UserbotService(
        settings, Storage(settings.db_path, SecretBox(settings.resolve_secret_key())), StubTransport
    )


def _service_over(settings: Settings, transport: StubTransport) -> UserbotService:
    """Сервис, все соединения которого идут через один и тот же транспорт.

    Заглушка держит журнал сообщений внутри экземпляра, поэтому обычная фабрика
    (новый объект на каждое подключение) «теряет» историю сервера при
    переподключении — а именно её и проверяют тесты добора.
    """
    return UserbotService(
        settings,
        Storage(settings.db_path, SecretBox(settings.resolve_secret_key())),
        lambda: transport,
    )


class UnbufferedStub(StubTransport):
    """Заглушка, которая не копит события до подписки.

    Обычная заглушка складывает входящие в очередь, и событие, пришедшее до
    подписки, всё равно доходит — гонка между добором и подпиской на ней не
    воспроизводится. Настоящий сервер такое событие просто не отдаёт.
    """

    def __init__(self) -> None:
        super().__init__()
        self._listening = False
        #: Сообщение, которое придёт ровно во время добора.
        self.race: tuple[str, str] | None = None

    async def events(self) -> AsyncIterator[Update]:
        self._listening = True
        async for update in super().events():
            yield update

    async def fetch_updates(
        self, cursor: str | None, limit: int
    ) -> tuple[list[Update], str | None]:
        result = await super().fetch_updates(cursor, limit)
        if self.race is not None:
            chat_id, text = self.race
            self.race = None
            await self.push_incoming(chat_id, text)
        return result

    async def push_incoming(self, chat_id: str, text: str, sender_id: str = "stub-peer") -> Message:
        if not self._listening:
            # Слушателя нет — событие проходит мимо потока и остаётся только в
            # журнале сервера.
            return self._record(chat_id, text, outgoing=False, sender_id=sender_id)
        return await super().push_incoming(chat_id, text, sender_id)


def _slow_reconnect(settings: Settings) -> Settings:
    """Задержка перед повтором, за которую тест успеет заглянуть в состояние.

    С мгновенным переподключением проверить «аккаунт не готов» невозможно: он
    успевает подняться раньше, чем тест дочитает состояние из базы.
    """
    return settings.model_copy(update={"reconnect_base_seconds": 0.3, "reconnect_max_seconds": 0.3})


def _delay_spy(attempts: list[int]) -> Callable[..., timedelta]:
    """Подменяет расчёт задержки, запоминая номер попытки.

    Проверяется именно номер: по нему видно, растёт ли экспонента или счётчик
    каждый раз сбрасывается в единицу.
    """

    def spy(attempt: int, base: float, maximum: float) -> timedelta:
        attempts.append(attempt)
        return real_backoff_delay(attempt=attempt, base=base, maximum=maximum)

    return spy


class StalledCursorStub(StubTransport):
    """Транспорт, который отдаёт события, но не двигает позицию.

    Контракт `fetch_updates` это запрещает. Ядро обязано заметить неисправность,
    а не перечитывать одну и ту же страницу до бесконечности.
    """

    async def fetch_updates(
        self, cursor: str | None, limit: int
    ) -> tuple[list[Update], str | None]:
        await super().fetch_updates(cursor, limit)
        return [self._record_update("chat-1", "по кругу", outgoing=False)], cursor


class EndingStreamStub(StubTransport):
    """Транспорт, у которого живой поток заканчивается сам, без ошибки.

    Так ведёт себя корректно закрытое сервером соединение: исключения нет, а
    событий больше не будет. Первый поток закрывается по команде теста, когда
    аккаунт уже поднялся: поток, умерший до готовности, — это неуспешное
    подключение, а не обрыв работающего соединения, и проверяется отдельно
    (см. `test_stream_dying_during_backfill_keeps_account_unready`).
    """

    def __init__(self) -> None:
        super().__init__()
        self.streams = 0
        #: Команда закрыть первый — уже работающий — поток.
        self.close_first = asyncio.Event()

    async def events(self) -> AsyncIterator[Update]:
        self.streams += 1
        if self.streams == 1:
            await self.close_first.wait()
            return
        if self.streams == 2:
            return
        async for update in super().events():
            yield update


class DyingStreamStub(StubTransport):
    """Транспорт, у которого живой поток умирает ровно во время добора.

    Момент выбран худший: подписка открыта, добор уже идёт и вот-вот упрётся в
    пустую страницу — то есть «догнал текущий момент». Объявить аккаунт готовым
    после этого нельзя: слушать сервер больше некому.
    """

    def __init__(self) -> None:
        super().__init__()
        self.streams = 0
        #: Сколько ближайших потоков умрут, не дождавшись конца добора.
        self.dying_streams = 0
        #: Чем именно умрёт поток; `None` — генератор просто закончится.
        self.die_with: Exception | None = TransportOutcomeUnknown("соединение оборвалось")
        #: Сообщение, которое «придёт», пока соединения нет.
        self.missed: tuple[str, str] | None = None
        self._doomed = False
        self._dying = asyncio.Event()

    async def events(self) -> AsyncIterator[Update]:
        self.streams += 1
        if self.dying_streams > 0:
            self.dying_streams -= 1
            self._doomed = True
            await self._dying.wait()
            self._dying.clear()
            if self.die_with is not None:
                raise self.die_with
            return
        async for update in super().events():
            yield update

    async def fetch_updates(
        self, cursor: str | None, limit: int
    ) -> tuple[list[Update], str | None]:
        if self._doomed:
            self._doomed = False
            self._dying.set()
            # Пауза настоящая: проверяется гонка добора со смертью потока, а не
            # порядок вызовов внутри одного шага планировщика.
            await asyncio.sleep(0.05)
        return await super().fetch_updates(cursor, limit)

    async def disconnect(self) -> None:
        await super().disconnect()
        if self.missed is not None:
            chat_id, text = self.missed
            self.missed = None
            # Сообщение приходит на сервер, пока клиент переподключается:
            # добраться до него можно только по курсору.
            self.add_missed(chat_id, text)


class FlakyConnectStub(StubTransport):
    """Заглушка, которой можно запретить подключаться заданное число раз."""

    def __init__(self) -> None:
        super().__init__()
        #: Сколько ближайших подключений завершатся ошибкой.
        self.failures = 0
        self.connects = 0

    async def connect(self, session: Session) -> Session | None:
        self.connects += 1
        if self.failures > 0:
            self.failures -= 1
            raise TransportOutcomeUnknown("сеть недоступна")
        return await super().connect(session)


async def _login(service: UserbotService, phone: str = "+79990000000") -> int:
    account = await service.add_account(phone)
    challenge_id = await service.start_login(account.id)
    await service.complete_login(challenge_id, STUB_CODE)
    return account.id


async def _wait(condition, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if await condition():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("условие не выполнилось за отведённое время")


async def test_stale_sending_is_failed_not_resent(settings: Settings) -> None:
    """Запись, застрявшая в sending, не переотправляется вслепую."""
    storage = Storage(settings.db_path, SecretBox(settings.resolve_secret_key()))
    await storage.open()
    account = await storage.add_account("+79990000000", None)
    await storage.enqueue(account.id, "chat-1", "в полёте", "ключ-1", 60.0)
    claimed = await storage.claim_queued()
    await storage.mark_sending(claimed[0].id)
    item = await storage.get_outbox(claimed[0].id)
    assert item is not None and item.state is OutboxState.SENDING
    await storage.close()

    service = _new_service(settings)
    await service.start()
    try:
        item = await service._storage.get_outbox_by_key("ключ-1")
        assert item is not None
        assert item.state is OutboxState.FAILED
        assert "исход отправки неизвестен" in (item.error or "")
    finally:
        await service.stop()


async def test_claim_is_atomic(settings: Settings) -> None:
    """Два одновременных захвата не получают одну и ту же запись."""
    storage = Storage(settings.db_path, SecretBox(settings.resolve_secret_key()))
    await storage.open()
    account = await storage.add_account("+79990000000", None)
    for index in range(10):
        await storage.enqueue(account.id, "chat-1", f"сообщение {index}", f"ключ-{index}", 60.0)

    first, second = await asyncio.gather(storage.claim_queued(20), storage.claim_queued(20))
    ids = [item.id for item in first] + [item.id for item in second]
    assert len(ids) == 10
    assert len(set(ids)) == 10
    await storage.close()


async def test_incoming_events_are_recorded_and_deduplicated(settings: Settings) -> None:
    """Входящие сообщения попадают в журнал, повтор того же id — не создаёт дубль."""
    service = _new_service(settings)
    await service.start()
    try:
        account_id = await _login(service)
        transport = service._connections.get(account_id)
        assert isinstance(transport, StubTransport)

        message = await transport.push_incoming("chat-5", "входящее")

        async def recorded() -> bool:
            events = await service.recent_events(limit=50, after_id=0)
            return any(e["kind"] == "message.received" for e in events)

        await _wait(recorded)

        # Тот же remote_id после «переподключения» не должен дублироваться.
        await transport._incoming.put(Update(message=message))
        await asyncio.sleep(0.3)
        events = await service.recent_events(limit=50, after_id=0)
        received = [e for e in events if e["kind"] == "message.received"]
        assert len(received) == 1
    finally:
        await service.stop()


async def test_dedup_window_allows_later_repeat(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Без nonce повтор блокируется только в пределах окна, а не навсегда."""
    monkeypatch.setattr(service_module, "DEDUP_WINDOW_SECONDS", 0.0)
    service = _new_service(settings)
    await service.start()
    try:
        account_id = await _login(service)
        first, created_first = await service.enqueue_message(account_id, "chat-1", "повтор")
        second, created_second = await service.enqueue_message(account_id, "chat-1", "повтор")
        assert created_first is True
        assert created_second is True
        assert first.id != second.id
    finally:
        await service.stop()


async def test_nonce_dedups_regardless_of_window(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """С явным nonce клиент управляет идемпотентностью, окно не действует."""
    monkeypatch.setattr(service_module, "DEDUP_WINDOW_SECONDS", 0.0)
    service = _new_service(settings)
    await service.start()
    try:
        account_id = await _login(service)
        first, _ = await service.enqueue_message(account_id, "chat-1", "раз", nonce="n-1")
        second, created = await service.enqueue_message(account_id, "chat-1", "раз", nonce="n-1")
        assert created is False
        assert first.id == second.id
    finally:
        await service.stop()


async def test_secrets_are_not_world_readable(settings: Settings) -> None:
    """В БД лежат сессии аккаунтов — посторонние не должны её читать."""
    service = _new_service(settings)
    await service.start()
    try:
        await _login(service)
        assert settings.data_dir.stat().st_mode & 0o077 == 0
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(f"{settings.db_path}{suffix}")
            if candidate.exists():
                assert candidate.stat().st_mode & 0o077 == 0, candidate
        settings.resolve_token()
        assert settings.token_path.stat().st_mode & 0o077 == 0
    finally:
        await service.stop()


async def test_disabled_account_is_not_resumed(settings: Settings) -> None:
    """Остановку вручную рестарт демона отменять не должен."""
    service = _new_service(settings)
    await service.start()
    account_id = await _login(service)
    await service.disable_account(account_id, "тест")
    await service.stop()

    restarted = _new_service(settings)
    await restarted.start()
    try:
        accounts = await restarted.list_accounts()
        assert accounts[0].state is AccountState.DISABLED
    finally:
        await restarted.stop()


async def test_ready_account_is_resumed_after_restart(settings: Settings) -> None:
    """Готовый аккаунт поднимается сам при следующем старте демона."""
    service = _new_service(settings)
    await service.start()
    await _login(service)
    await service.stop()

    restarted = _new_service(settings)
    await restarted.start()
    try:
        accounts = await restarted.list_accounts()
        assert accounts[0].state is AccountState.READY
    finally:
        await restarted.stop()


async def test_live_stream_saves_transport_cursor(settings: Settings) -> None:
    """Позиция берётся из потока сервера, а не из идентификатора сообщения."""
    transport = StubTransport()
    service = _service_over(settings, transport)
    await service.start()
    try:
        account_id = await _login(service)
        message = await transport.push_incoming("chat-1", "живое")

        async def cursor_saved() -> bool:
            return await service._storage.load_cursor(account_id) is not None

        await _wait(cursor_saved)
        cursor = await service._storage.load_cursor(account_id)
        assert cursor is not None
        assert cursor != message.remote_id
        # Позиция настоящая: с неё добор ничего заново не притащит.
        updates, _ = await transport.fetch_updates(cursor, 10)
        assert updates == []
    finally:
        await service.stop()


async def test_backfill_after_reconnect_starts_from_saved_cursor(settings: Settings) -> None:
    """Пропущенное добирается, а уже полученное не приходит вторым событием."""
    transport = StubTransport()
    service = _service_over(settings, transport)
    await service.start()
    try:
        account_id = await _login(service)
        await transport.push_incoming("chat-1", "живое")

        async def cursor_saved() -> bool:
            return await service._storage.load_cursor(account_id) is not None

        await _wait(cursor_saved)

        # Пока соединения нет, на сервере копится пропущенное.
        session = Session.model_validate(await service._storage.load_session(account_id))
        await service._connections.disconnect(account_id)
        transport.add_missed("chat-1", "пропущенное")
        await service._connections.connect(account_id, session)

        events = await service.recent_events(limit=50, after_id=0)
        received = [e for e in events if e["kind"] == "message.received"]
        texts = [e["payload"]["text"] for e in received]  # type: ignore[index]
        assert texts == ["живое", "пропущенное"]
    finally:
        await service.stop()


async def test_message_arriving_during_backfill_is_not_lost(settings: Settings) -> None:
    """Событие в окне между добором и подпиской должно дойти."""
    transport = UnbufferedStub()
    transport.race = ("chat-7", "в окне")
    service = _service_over(settings, transport)
    await service.start()
    try:
        await _login(service)

        async def delivered() -> bool:
            events = await service.recent_events(limit=50, after_id=0)
            return any(e["payload"].get("text") == "в окне" for e in events)  # type: ignore[union-attr]

        await _wait(delivered)
    finally:
        await service.stop()


async def test_ended_stream_is_treated_as_disconnect(settings: Settings) -> None:
    """Поток, закрывшийся без ошибки, — это обрыв, а не повод снять надзор."""
    transport = EndingStreamStub()
    service = _service_over(settings, transport)
    await service.start()
    try:
        await _login(service)
        transport.close_first.set()

        async def reconnected() -> bool:
            # Два потока закрылись сразу, третий остался жить.
            return transport.streams >= 3

        await _wait(reconnected)

        async def ready() -> bool:
            accounts = await service.list_accounts()
            return accounts[0].state is AccountState.READY

        await _wait(ready)
        assert service._connections.active == 1
    finally:
        await service.stop()


async def _login_then_stop(settings: Settings, transport: StubTransport) -> None:
    """Заводит аккаунт и гасит демон: дальше проверяется подъём при старте."""
    service = _service_over(settings, transport)
    await service.start()
    await _login(service)
    await service.stop()


def _watch_states(service: UserbotService) -> list[tuple[AccountState, list[str]]]:
    """Запоминает переходы состояния вместе с журналом на тот момент.

    По итоговому состоянию проверить нечего: мёртвый поток переоткрывается за
    миллисекунды, и «готов без подписки» снаружи неотличимо от «просто готов».
    Значение имеет момент объявления: чем именно аккаунт был готов, когда его
    таким назвали.
    """
    seen: list[tuple[AccountState, list[str]]] = []
    original = service._storage.set_account_state

    async def record(account_id: int, state: AccountState, error: str | None = None) -> None:
        events = await service.recent_events(limit=100, after_id=0)
        texts = [
            e["payload"].get("text")  # type: ignore[union-attr]
            for e in events
            if e["kind"] == "message.received"
        ]
        seen.append((state, texts))
        await original(account_id, state, error)

    service._storage.set_account_state = record  # type: ignore[method-assign]
    return seen


async def _wait_ready(service: UserbotService) -> None:
    async def ready() -> bool:
        accounts = await service.list_accounts()
        return accounts[0].state is AccountState.READY

    await _wait(ready)


async def test_stream_dying_during_backfill_keeps_account_unready(settings: Settings) -> None:
    """Смерть потока во время добора — неуспешное подключение, а не готовность."""
    transport = DyingStreamStub()
    await _login_then_stop(settings, transport)

    transport.dying_streams = 1
    restarted = _service_over(_slow_reconnect(settings), transport)
    seen = _watch_states(restarted)
    await restarted.start()
    try:
        # Добор догнал текущий момент, но слушателя у сервера уже нет: готовым
        # такой аккаунт не объявляют — ни разу, даже на миг.
        assert AccountState.READY not in [state for state, _ in seen]
        accounts = await restarted.list_accounts()
        assert accounts[0].state is not AccountState.READY

        # Готовность приходит от надзорного цикла, со второй подпиской.
        await _wait_ready(restarted)
        assert transport.streams >= 2
    finally:
        await restarted.stop()


async def test_stream_ending_during_backfill_keeps_account_unready(settings: Settings) -> None:
    """Штатно закончившийся поток — тот же неуспех, что и упавший с ошибкой."""
    transport = DyingStreamStub()
    transport.die_with = None
    await _login_then_stop(settings, transport)

    transport.dying_streams = 1
    restarted = _service_over(_slow_reconnect(settings), transport)
    seen = _watch_states(restarted)
    await restarted.start()
    try:
        assert AccountState.READY not in [state for state, _ in seen]
        accounts = await restarted.list_accounts()
        assert accounts[0].state is not AccountState.READY

        await _wait_ready(restarted)
        assert transport.streams >= 2
    finally:
        await restarted.stop()


async def test_message_after_stream_death_is_not_lost(settings: Settings) -> None:
    """Окно между смертью потока и переподключением не теряет сообщений."""
    transport = DyingStreamStub()
    await _login_then_stop(settings, transport)

    transport.dying_streams = 1
    # Сообщение приходит уже после того, как поток умер: живым потоком его не
    # получить, остаётся добор по курсору при следующей подписке.
    transport.missed = ("chat-9", "в окне потери")
    restarted = _service_over(settings, transport)
    seen = _watch_states(restarted)
    await restarted.start()
    try:
        await _wait_ready(restarted)
        # Готовность означает «всё пропущенное уже в журнале». Сообщение из
        # окна потери обязано быть там к этому моменту, а не когда-нибудь
        # потом: обещание готовности иначе ничего не стоит.
        first_ready = next(texts for state, texts in seen if state is AccountState.READY)
        assert "в окне потери" in first_ready
    finally:
        await restarted.stop()


async def test_stalled_cursor_stops_backfill(settings: Settings) -> None:
    """Неподвижная позиция при непустой порции — неисправность, а не готовность."""
    transport = StalledCursorStub()
    service = _service_over(settings, transport)
    await service.start()
    try:
        with pytest.raises(BackfillStalled):
            await _login(service)
        accounts = await service.list_accounts()
        assert accounts[0].state is not AccountState.READY
    finally:
        await service.stop()


async def test_reconnect_delay_grows_between_attempts(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Повторные неудачи наращивают задержку, а не топчутся на минимальной."""
    attempts: list[int] = []

    transport = FlakyConnectStub()
    service = _service_over(settings, transport)
    await service.start()
    await _login(service)
    await service.stop()

    # Подключиться больше не даём: интересна только последовательность попыток.
    transport.failures = 1000
    monkeypatch.setattr(supervisor_module, "backoff_delay", _delay_spy(attempts))
    restarted = _service_over(settings, transport)
    await restarted.start()
    try:

        async def three_attempts() -> bool:
            return len(attempts) >= 3

        await _wait(three_attempts)
        assert attempts[:3] == [1, 2, 3]
    finally:
        await restarted.stop()


async def test_account_failed_at_startup_keeps_retrying(settings: Settings) -> None:
    """Аккаунт, не поднявшийся при старте демона, не застревает в backoff."""
    transport = FlakyConnectStub()
    service = _service_over(settings, transport)
    await service.start()
    await _login(service)
    await service.stop()

    transport.failures = 2
    restarted = _service_over(settings, transport)
    await restarted.start()
    try:

        async def ready() -> bool:
            accounts = await restarted.list_accounts()
            return accounts[0].state is AccountState.READY

        await _wait(ready)
        # Успех пришёл именно через повторы надзорного цикла.
        assert transport.failures == 0
        assert transport.connects >= 4
    finally:
        await restarted.stop()


async def test_rotated_token_is_persisted(settings: Settings) -> None:
    """Новый токен от сервера должен пережить перезапуск демона.

    Раньше `connect` ничего не возвращал: адаптер получал ротацию, а сказать о
    ней ядру было нечем. В базе оставался прежний токен, и следующий запуск
    просил войти заново сразу после успешного входа.
    """
    transport = StubTransport()
    service = _service_over(settings, transport)
    await service.start()
    try:
        await _login(service)
        stored = await service._storage.load_session(1)
        assert stored is not None
        before = str(stored["token"])

        # Имитируем ротацию на стороне сервера при переподключении.
        transport.rotate_token_on_connect = True
        await service._connections.disconnect(1)
        await service._connections.connect(1, Session.model_validate(stored))

        refreshed = await service._storage.load_session(1)
        assert refreshed is not None
        assert str(refreshed["token"]) != before
    finally:
        await service.stop()


async def test_rotated_token_on_first_login_is_persisted(settings: Settings) -> None:
    """Ротация на самом первом подключении после входа тоже должна сохраниться.

    Вход сохраняет сессию сам, уже после подключения, и записывал ту, что
    передал, — затирая новый токен, полученный на этом же подключении. В памяти
    оставался новый, в базе старый.
    """
    transport = StubTransport()
    service = _service_over(settings, transport)
    await service.start()
    try:
        account = await service.add_account("+79990000000")
        challenge_id = await service.start_login(account.id)
        transport.rotate_token_on_connect = True
        await service.complete_login(challenge_id, STUB_CODE)

        stored = await service._storage.load_session(account.id)
        assert stored is not None
        # Транспорт выдал новый токен вместо того, что пришёл из входа.
        assert not str(stored["token"]).startswith("stub-out")
        active = service._connections._sessions[account.id]
        assert active.token == str(stored["token"])
    finally:
        await service.stop()


async def test_supervisor_reports_backoff_to_subscribers(settings: Settings) -> None:
    """Обрыв связи виден в живом потоке, а не только в опросе статуса.

    Обрывы и потеря авторизации случаются именно здесь, под надзором. Пока надзор
    писал состояние мимо общего пути, подписчик о них не узнавал вовсе — то есть
    молчал ровно о том, ради чего поток и слушают.
    """
    transport = EndingStreamStub()
    service = _service_over(settings, transport)
    await service.start()
    try:
        await _login(service)
        transport.close_first.set()

        async def announced() -> bool:
            events = await service.recent_events(limit=100, after_id=0)
            return any(event["kind"] == "account.backoff" for event in events)

        await _wait(announced)
    finally:
        await service.stop()
