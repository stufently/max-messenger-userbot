"""Транспорт поверх библиотеки PyMax (PyPI ``maxapi-python``, импорт ``pymax``).

**Адаптер не проверен на живом аккаунте MAX.** Проверены исходники библиотеки
(2.3.1), поведение на подставном модуле и разговор по протоколу с локальным
websocket-сервером (`tests/test_transport_ws.py`): handshake, вход по
сохранённому токену, входящее событие, отправка и закрытие соединения идут через
настоящий сокет. Чего это не заменяет — самого MAX. С настоящим аккаунтом в
первую очередь нужно проверить, в порядке убывания цены ошибки:

1. Раскладку отказов сервера ([pymax_errors][maxub.transport.pymax_errors]):
   коды ошибок MAX не документированы, маркеры лимитов и авторизации подобраны
   по смыслу. Пока код не опознан, отказ считается неизвестным исходом, и
   сообщение уходит человеку, а не повторяется.
2. Вход по SMS и по QR целиком: сколько попыток ввода кода даёт сервер, когда
   истекает QR, что приходит при 2FA.
3. Порядок и полноту `fetch_history`, наличие `chat_id` у входящих событий.
4. Живучесть соединения: правда ли смерть `client.start()` — единственный
   признак обрыва, и не молчит ли PyMax при полуживом сокете.

Чего адаптер не умеет — перечислено в `PyMaxTransport.capabilities`. Главное
ограничение: серверной позиции в потоке обновлений PyMax не даёт, поэтому
пропущенное за время простоя не добирается — события, пришедшие без
подключения, теряются безвозвратно.

Ротацию токена адаптер отдаёт ядру: MAX может выдать новый токен при очередном
входе, PyMax сообщает его хранилищу, а `connect` возвращает обновлённую сессию —
ядро сохраняет её сразу, поэтому следующий запуск не попросит войти заново.
А вот `sync`-состояние в конверт по-прежнему не кладётся: обновлять его негде, а
протухший маркер хуже отсутствующего — по нему MAX не пришлёт то, что мы ещё не
видели.

Устройство: вход живёт в [pymax_auth][maxub.transport.pymax_auth], сборка
клиента — в [pymax_client][maxub.transport.pymax_client], фоновая жизнь
соединения — в [pymax_runtime][maxub.transport.pymax_runtime], раскладка
ошибок — в [pymax_errors][maxub.transport.pymax_errors].
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import replace
from typing import Any, TypeVar

from maxub.core.models import LoginChallenge, Message, QrChallenge, Session
from maxub.transport.base import (
    Capabilities,
    ReconcileOutcome,
    ReconcileResult,
    TransportAuthError,
    TransportNotApplied,
    TransportOutcomeUnknown,
    TransportUnsupported,
    Update,
)
from maxub.transport.pymax_auth import LoginFlows
from maxub.transport.pymax_client import as_chat_id, build_extra_config, load_pymax
from maxub.transport.pymax_errors import translate
from maxub.transport.pymax_login import RefuseAuth
from maxub.transport.pymax_runtime import ClientRuntime
from maxub.transport.pymax_session import Envelope, decode, encode

_T = TypeVar("_T")

#: Сколько ждать, пока сохранённая сессия поднимет соединение.
CONNECT_WAIT = 60.0

#: PyMax просит каталог под свой файл сессий, но хранилище подменено на
#: перехват в памяти, и до диска дело не доходит.
WORK_DIR = "."


class PyMaxTransport:
    """Один аккаунт MAX через PyMax."""

    name = "pymax"

    capabilities = Capabilities(
        send_text=True,
        fetch_history=True,
        # PyMax умеет и правку, и удаление, и вложения, но в контракте
        # транспорта таких методов нет. Объявить их здесь значило бы пообещать
        # ядру вызовы, которых оно всё равно не сделает.
        edit_message=False,
        delete_message=False,
        media=False,
        # Серверной позиции в потоке обновлений у PyMax нет: история берётся по
        # чату и времени, живой поток приходит колбэками без меток. Курсор
        # взять неоткуда, а подставить идентификатор сообщения нельзя — это
        # другое пространство значений.
        backfill=False,
        # Клиентский идентификатор сообщения PyMax генерирует сам, внутри
        # `MessageService._next_cid`; передать свой токен на сервер нечем.
        # Значит, опознать отправленное после обрыва невозможно.
        reconcile=False,
        qr_login=True,
    )

    def __init__(
        self,
        *,
        proxy: str | None = None,
        request_timeout: float = 30.0,
        endpoint: str | None = None,
    ) -> None:
        self._pymax = load_pymax()
        self._proxy = proxy
        self._request_timeout = request_timeout
        self._endpoint = endpoint
        self._runtime: ClientRuntime | None = None
        self._login = LoginFlows(self._pymax, self._launch, WORK_DIR)

    # --- авторизация --------------------------------------------------------

    async def start_login(self, phone: str) -> LoginChallenge:
        return await self._closing_on_failure(self._login.start_phone(phone))

    async def complete_login(self, challenge_id: str, code: str, account_id: int) -> Session:
        # Единственный шаг без уборки: неверный код оставляет вход живым, и
        # закрыть здесь клиент значило бы отнять у пользователя вторую попытку.
        return await self._login.complete_phone(challenge_id, code, account_id)

    async def start_qr_login(self) -> QrChallenge:
        return await self._closing_on_failure(self._login.start_qr())

    async def poll_qr_login(self, challenge_id: str, account_id: int) -> Session | None:
        if not self._login.owns_qr(challenge_id):
            # Уборкой отвечать нельзя: закрыли бы чужое живое соединение.
            raise TransportAuthError("запрос QR-входа не найден, начните вход заново")
        return await self._closing_on_failure(self._login.poll_qr(challenge_id, account_id))

    # --- соединение ---------------------------------------------------------

    async def connect(self, session: Session) -> Session | None:
        envelope = decode(session.token)
        web = envelope.kind == "web"
        runtime = await self._launch(
            lambda extra: self._build_client(session, extra, web=web),
            web=web,
            envelope=envelope,
            device_id=session.device_id,
        )
        # `during_auth`: отказ сервера на этом шаге — это отвергнутая сессия, а
        # не сбой отправки, и ядру нужен именно `AUTH_REQUIRED`.
        await self._closing_on_failure(
            runtime.await_event(runtime.started, CONNECT_WAIT, during_auth=True)
        )
        return self._refreshed(session, envelope, runtime)

    @staticmethod
    def _refreshed(session: Session, envelope: Envelope, runtime: Any) -> Session | None:
        """Отдаёт сессию с новым токеном, если сервер выдал его при входе.

        Ротацию ловит хранилище (`MemorySessionStore.update_token`) — наружу
        PyMax токен не возвращает. Без этой сверки новый токен остался бы жить
        только в памяти процесса: ядро сохранило бы прежний, и следующий запуск
        попросил бы войти заново.
        """
        stored = getattr(runtime.store.saved, "token", None)
        if not stored or stored == envelope.token:
            return None
        return session.model_copy(update={"token": encode(replace(envelope, token=str(stored)))})

    def _build_client(self, session: Session, extra: Any, *, web: bool) -> Any:
        """Клиент выбирается по тому, каким входом получена сессия.

        Токен веб-входа живёт с веб-подписью устройства: подключаться им по TCP
        как «андроидом» — значит заявить серверу другое устройство на той же
        сессии.
        """
        if web:
            return self._pymax.WebClient(
                work_dir=WORK_DIR, extra_config=extra, auth_flow=RefuseAuth()
            )
        return self._pymax.Client(
            phone=session.phone, work_dir=WORK_DIR, extra_config=extra, auth_flow=RefuseAuth()
        )

    async def disconnect(self) -> None:
        runtime, self._runtime = self._runtime, None
        self._login.reset()
        if runtime is not None:
            await runtime.close()

    # --- сообщения ----------------------------------------------------------

    async def send_text(self, chat_id: str, text: str, client_token: str) -> str:
        """Отправляет текст.

        ``client_token`` до сервера не доходит: клиентский идентификатор
        сообщения PyMax генерирует сам и параметра для чужого не имеет. Значит,
        после обрыва связи опознать отправленное невозможно — `reconcile`
        выключен, и неоднозначная отправка уходит человеку, а не повторяется
        автоматически. Чтобы это исправить, PyMax нужен необязательный ``cid``
        в `send_message`.

        Разметка: PyMax разбирает текст как markdown, поэтому символы разметки
        меняют вид сообщения. Проверить на живом аккаунте.
        """
        client = self._require_live()
        try:
            message = await client.send_message(as_chat_id(chat_id), text)
        except Exception as exc:
            raise translate(exc) from exc
        if message is None:
            # Запрос ушёл, ответ пришёл, а сообщения в нём нет. Считать это
            # неудачей нельзя: оно могло уйти получателю.
            raise TransportOutcomeUnknown("MAX не вернул отправленное сообщение")
        return str(message.id)

    async def fetch_history(self, chat_id: str, limit: int) -> list[Message]:
        client = self._require_live()
        runtime = self._require_runtime()
        try:
            raw = await client.fetch_history(chat_id=as_chat_id(chat_id), backward=limit)
        except Exception as exc:
            raise translate(exc) from exc
        found = (runtime.to_message(item) for item in raw or [])
        messages = [item for item in found if item is not None]
        # Порядок ответа MAX не описан, поэтому задаётся здесь явно: ядро ждёт
        # хронологию, а не то, как легло у сервера.
        messages.sort(key=lambda item: item.timestamp)
        return messages[-limit:] if limit > 0 else messages

    async def fetch_updates(
        self, cursor: str | None, limit: int
    ) -> tuple[list[Update], str | None]:
        raise TransportUnsupported(
            "PyMax не сообщает позицию в потоке обновлений: добор пропущенного невозможен"
        )

    async def reconcile_send(self, chat_id: str, client_token: str) -> ReconcileResult:
        """Всегда «выяснить не удалось» — и это единственный честный ответ.

        Искать нечего: клиентский токен на сервер не уходил, а по тексту и
        времени сообщение не опознать — два одинаковых сообщения подряд
        неразличимы. `NOT_FOUND` был бы выдумкой, за которую платит получатель:
        ядро повторило бы отправку и создало дубль.
        """
        return ReconcileResult(
            outcome=ReconcileOutcome.INCONCLUSIVE,
            detail="PyMax не передаёт клиентский токен на сервер, сверка невозможна",
        )

    async def events(self) -> AsyncIterator[Update]:
        runtime = self._require_runtime()
        while True:
            update = await runtime.next_update()
            if update is None:
                # Соединение закрыто без ошибки. Ядро само превратит конец
                # потока в обрыв и поднимет соединение заново.
                return
            yield update

    # --- внутреннее ---------------------------------------------------------

    async def _launch(
        self,
        build: Callable[[Any], Any],
        *,
        web: bool,
        envelope: Envelope | None = None,
        device_id: str | None = None,
    ) -> ClientRuntime:
        """Поднимает новый клиент, закрывая прежний.

        Прежний закрывается всегда: экземпляр транспорта обслуживает один
        аккаунт, и два живых клиента на нём означали бы два соединения,
        наперегонки читающих один и тот же поток.
        """
        await self.disconnect()
        runtime = ClientRuntime()
        extra = build_extra_config(
            self._pymax,
            runtime,
            web=web,
            proxy=self._proxy,
            request_timeout=self._request_timeout,
            envelope=envelope,
            device_id=device_id,
            endpoint=self._endpoint,
        )
        runtime.launch(build(extra))
        self._runtime = runtime
        return runtime

    async def _closing_on_failure(self, step: Awaitable[_T]) -> _T:
        """Не оставляет за собой живой клиент, если шаг не удался.

        Неудачный вход и отвергнутая сессия иначе оставили бы соединение с MAX
        открытым: транспорт считает себя незанятым, а сокет и фоновая задача
        живут до перезапуска демона.
        """
        try:
            return await step
        except Exception:
            await self.disconnect()
            raise

    def _require_runtime(self) -> ClientRuntime:
        runtime = self._runtime
        if runtime is None:
            raise TransportNotApplied("транспорт pymax не подключён")
        return runtime

    def _require_live(self) -> Any:
        """Проверяет своё состояние до обращения к сети.

        Это единственный случай, когда невыполнение действительно доказано:
        запрос не дошёл даже до библиотеки, поэтому повтор безопасен.
        """
        runtime = self._require_runtime()
        if not runtime.alive or runtime.client is None:
            raise TransportNotApplied("соединение pymax закрыто")
        return runtime.client
