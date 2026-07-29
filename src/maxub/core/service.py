"""Ядро: сервисы приложения.

Не зависит ни от FastAPI, ни от Typer, ни от моделей конкретной транспортной
библиотеки — они остаются адаптерами по краям. Соединения ведёт
[ConnectionManager][maxub.core.sync.ConnectionManager], очередь разбирает
[OutboxWorker][maxub.core.sender.OutboxWorker], здесь — только состав операций.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging

from maxub.config import Settings
from maxub.core.auth import LoginError, LoginService, TooManyChallenges
from maxub.core.events import EventBus
from maxub.core.housekeeping import Housekeeper
from maxub.core.manual_retry import ManualRetry
from maxub.core.models import (
    Account,
    AccountState,
    Event,
    OutboxItem,
    OutboxState,
    QrStatus,
    Session,
)
from maxub.core.ports import TransportFactory
from maxub.core.ratelimit import RateLimiter
from maxub.core.review import OutboxItemBusy, OutboxItemNotFound
from maxub.core.sender import SEND_ACTION, OutboxWorker
from maxub.core.storage import DuplicateAccountError, Storage
from maxub.core.sync import ConnectionManager
from maxub.transport.base import Capabilities, TransportAuthError, TransportUnsupported

log = logging.getLogger(__name__)

DEDUP_WINDOW_SECONDS = 60.0

# Состояния, из которых сообщение само уже не выберется: отказавшие записи ждут
# решения человека, «в полёте» — разбора при следующем запуске демона. Остальные
# движутся сами, и показывать их как «застрявшее» значило бы звать человека туда,
# где он не нужен.
STUCK_STATES = (OutboxState.FAILED, OutboxState.SENDING)

# Состояния, из которых аккаунт поднимается сам при старте демона. DISABLED
# сюда не входит: остановку вручную процесс отменять не должен.
RESUMABLE_STATES = frozenset(
    {
        AccountState.READY,
        AccountState.SYNCING,
        AccountState.CONNECTING,
        AccountState.BACKOFF,
    }
)


class ServiceError(Exception):
    """Ошибка прикладного уровня, пригодная для показа пользователю."""


class ServiceOverloaded(ServiceError):
    """Отказ по исчерпанному ресурсу: повторить позже осмысленно.

    Отделено от общей ошибки ради адаптеров: перегрузка и «не найдено» должны
    выглядеть по-разному и для HTTP, и для человека за CLI.
    """


class ServiceNotFound(ServiceError):
    """Объекта с таким идентификатором нет.

    Отделено от конфликта состояния по той же причине: скрипт различает «такой
    записи нет» и «эта запись сейчас недоступна» по коду выхода, а не по тексту.
    """


def idempotency_key(account_id: int, chat_id: str, text: str, nonce: str | None) -> str:
    """Ключ идемпотентности.

    Поля разделяются длиной, а не символом-разделителем: иначе значения со
    служебным символом внутри давали бы одинаковый ключ для разных сообщений.
    """
    parts = [str(account_id), chat_id, text, nonce or ""]
    raw = "".join(f"{len(part)}:{part}" for part in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class UserbotService:
    def __init__(
        self,
        settings: Settings,
        storage: Storage,
        transport_factory: TransportFactory,
    ) -> None:
        self._settings = settings
        self._storage = storage
        self._events = EventBus()
        self._limiter = RateLimiter(
            rate_per_minute=settings.send_rate_per_minute,
            burst=settings.send_burst,
            jitter_seconds=settings.send_jitter_seconds,
        )
        self._connections = ConnectionManager(storage, transport_factory, settings, self._emit)
        self._worker = OutboxWorker(
            repo=storage,
            limiter=self._limiter,
            get_transport=self._connections.get,
            settings=settings,
            publish=self._events.publish,
            on_auth_lost=self._on_auth_lost,
        )
        self._manual_retry = ManualRetry(storage, self._connections.get, self._events.publish)
        self._login = LoginService(storage, self._connections)
        self._housekeeper = Housekeeper(
            storage,
            retention_days=settings.events_retention_days,
            interval_seconds=settings.housekeeping_interval_seconds,
        )
        self._worker_task: asyncio.Task[None] | None = None
        self._housekeeping_task: asyncio.Task[None] | None = None

    # --- жизненный цикл -----------------------------------------------------

    async def start(self) -> None:
        await self._storage.open()
        for account_id, action, until in await self._storage.load_penalties():
            self._limiter.restore(account_id, action, until)
        for account in await self._storage.list_accounts():
            if account.state in RESUMABLE_STATES:
                await self._resume(account)
        # Сверка выполняется после восстановления соединений: без транспорта
        # спросить сервер об исходе отправки невозможно.
        await self._worker.reconcile_stale()
        self._worker_task = asyncio.create_task(self._worker.run())
        self._housekeeping_task = asyncio.create_task(self._housekeeper.run())

    async def _resume(self, account: Account) -> None:
        payload = await self._storage.load_session(account.id)
        if payload is None:
            await self._storage.set_account_state(account.id, AccountState.AUTH_REQUIRED)
            return
        session = Session.model_validate(payload)
        try:
            await self._connections.connect(account.id, session)
        except TransportAuthError:
            # Состояние auth_required уже выставлено. Повторять нечего: нужен
            # новый вход, и попытки только жгли бы отозванную сессию.
            return
        except Exception as exc:
            # Раньше аккаунт оставался в backoff без надзора и не поднимался до
            # перезапуска демона. Сеть при старте могла быть ещё не готова —
            # надзорный цикл продолжит попытки с растущей задержкой сам.
            log.warning("не удалось переподключить аккаунт %s: %s", account.id, exc)
            await self._storage.set_account_state(account.id, AccountState.BACKOFF, str(exc))
            self._connections.supervise(account.id, session)

    async def stop(self) -> None:
        self._worker.stop()
        self._housekeeper.stop()
        for task in (self._worker_task, self._housekeeping_task):
            if task is None:
                continue
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await self._connections.shutdown()
        await self._storage.close()

    # --- аккаунты -----------------------------------------------------------

    async def add_account(self, phone: str, label: str | None = None) -> Account:
        try:
            return await self._storage.add_account(phone, label)
        except DuplicateAccountError as exc:
            raise ServiceError(str(exc)) from exc

    async def list_accounts(self) -> list[Account]:
        return await self._storage.list_accounts()

    async def disable_account(self, account_id: int, reason: str) -> Account:
        await self._require_account(account_id)
        await self._connections.disconnect(account_id)
        await self._storage.set_account_state(account_id, AccountState.DISABLED, reason)
        return await self._require_account(account_id)

    async def _on_auth_lost(self, account_id: int, reason: str) -> None:
        await self._storage.set_account_state(account_id, AccountState.AUTH_REQUIRED, reason)

    # --- авторизация --------------------------------------------------------

    async def start_login(self, account_id: int) -> str:
        """Запрашивает код подтверждения на телефон."""
        account = await self._require_account(account_id)
        try:
            return await self._login.start_phone(account)
        except TooManyChallenges as exc:
            raise ServiceOverloaded(str(exc)) from exc
        except LoginError as exc:
            # Отказ входа — это ответ пользователю, а не сбой демона: без
            # обёртки он превратился бы в 500.
            raise ServiceError(str(exc)) from exc

    async def complete_login(self, challenge_id: str, code: str) -> Account:
        try:
            account_id, session = await self._login.complete_phone(challenge_id, code)
        except LoginError as exc:
            raise ServiceError(str(exc)) from exc
        return await self._activate(account_id, session)

    async def start_qr_login(self, account_id: int) -> dict[str, object]:
        """Второй способ входа: код сканируется приложением MAX, SMS не нужен."""
        await self._require_account(account_id)
        try:
            return await self._login.start_qr(account_id)
        except TooManyChallenges as exc:
            raise ServiceOverloaded(str(exc)) from exc
        except LoginError as exc:
            raise ServiceError(str(exc)) from exc

    async def poll_qr_login(self, challenge_id: str) -> tuple[QrStatus, Account | None]:
        try:
            status_value, account_id, session = await self._login.poll_qr(challenge_id)
        except LoginError as exc:
            raise ServiceError(str(exc)) from exc
        if session is None or account_id is None:
            return status_value, None
        return status_value, await self._activate(account_id, session)

    async def _activate(self, account_id: int, session: Session) -> Account:
        """Подключает аккаунт с новой сессией и только потом сохраняет её.

        Порядок важен: сохранить сначала — значит затереть рабочую сессию
        результатом неудачного входа. Обратный порядок в худшем случае теряет
        новую сессию при падении процесса, и аккаунт просто попросит войти
        заново — это дешевле потери доступа.

        Сохраняется именно та сессия, которую вернуло подключение: сервер мог
        выдать новый токен прямо на первом входе, и запись исходной затёрла бы
        его — в памяти остался бы новый, а в базе старый.
        """
        try:
            active = await self._connections.connect(account_id, session)
        except TransportAuthError as exc:
            raise ServiceError(str(exc)) from exc
        await self._storage.save_session(account_id, active.model_dump(mode="json"))
        return await self._require_account(account_id)

    # --- отправка -----------------------------------------------------------

    async def enqueue_message(
        self, account_id: int, chat_id: str, text: str, nonce: str | None = None
    ) -> tuple[OutboxItem, bool]:
        account = await self._require_account(account_id)
        if account.state is not AccountState.READY:
            raise ServiceError(f"аккаунт в состоянии {account.state.value}, отправка недоступна")
        if not self._capabilities(account_id).send_text:
            raise TransportUnsupported("транспорт не умеет отправлять текст")
        key = idempotency_key(account_id, chat_id, text, nonce)
        # С явным nonce дедупликация действует бессрочно: клиент сам управляет
        # идемпотентностью. Без него — только в пределах окна, иначе повторно
        # отправить тот же текст стало бы невозможно навсегда.
        window = float("inf") if nonce else DEDUP_WINDOW_SECONDS
        return await self._storage.enqueue(account_id, chat_id, text, key, window)

    async def list_stuck_messages(
        self, limit: int, state: OutboxState | None = None
    ) -> list[dict[str, object]]:
        """Записи, которые сами не уедут: отказавшие и застрявшие «в полёте».

        Отдаются целиком, вместе с ошибкой и числом попыток: человек решает по
        содержимому записи, а не по её идентификатору.
        """
        states = (state,) if state is not None else STUCK_STATES
        items = await self._storage.list_outbox(states, limit)
        return [item.model_dump(mode="json") for item in items]

    async def retry_message(self, item_id: int) -> dict[str, object]:
        """Повторяет отправку отказавшей записи по решению человека.

        Сообщение могло дойти до получателя — тогда повтор создаст дубль.
        Поэтому перед повтором демон сверяется с сервером, если транспорт это
        умеет, а исход сверки возвращается вызывающему вместе с признаком
        ``duplicate_risk``.
        """
        try:
            result = await self._manual_retry.retry(item_id)
        except OutboxItemNotFound as exc:
            raise ServiceNotFound(str(exc)) from exc
        except OutboxItemBusy as exc:
            raise ServiceError(str(exc)) from exc
        return result.model_dump(mode="json")

    async def discard_message(self, item_id: int, reason: str) -> dict[str, object]:
        """Закрывает отказавшую запись без отправки по решению человека.

        Второй исход ручного разбора рядом с повтором: часть застрявших
        сообщений отправлять уже не нужно — устарели, ушли другим способом,
        поставлены по ошибке. Без этого решения они оставались бы в списке
        навсегда и прятали бы в нём те записи, которыми ещё стоит заняться.
        """
        try:
            item = await self._manual_retry.discard(item_id, reason)
        except OutboxItemNotFound as exc:
            raise ServiceNotFound(str(exc)) from exc
        except OutboxItemBusy as exc:
            raise ServiceError(str(exc)) from exc
        return item.model_dump(mode="json")

    async def fetch_history(
        self, account_id: int, chat_id: str, limit: int
    ) -> list[dict[str, object]]:
        await self._require_account(account_id)
        if not self._capabilities(account_id).fetch_history:
            raise TransportUnsupported("транспорт не умеет выгружать историю")
        transport = self._connections.ensure(account_id)
        messages = await transport.fetch_history(chat_id, limit)
        return [m.model_dump(mode="json") for m in messages]

    # --- события ------------------------------------------------------------

    async def _emit(self, event: Event) -> None:
        """Публикует событие, если оно ещё не записано.

        Проверку дубликата делает журнал: после переподключения сервер вполне
        может выдать те же события заново.
        """
        if not await self._storage.record_event(event):
            return
        self._events.publish(event)

    def subscribe(self) -> asyncio.Queue[Event]:
        return self._events.subscribe()

    def unsubscribe(self, queue: asyncio.Queue[Event]) -> None:
        self._events.unsubscribe(queue)

    async def recent_events(self, limit: int, after_id: int) -> list[dict[str, object]]:
        rows = await self._storage.list_events(limit=limit, after_id=after_id)
        return [{"id": row_id, **event.model_dump(mode="json")} for row_id, event in rows]

    # --- статус -------------------------------------------------------------

    async def status(self) -> dict[str, object]:
        accounts = await self._storage.list_accounts()
        return {
            "transport": self._settings.transport,
            "accounts_total": len(accounts),
            "accounts_ready": sum(1 for a in accounts if a.state is AccountState.READY),
            "connections": self._connections.active,
            "outbox": await self._storage.outbox_stats(),
            "send_action": SEND_ACTION,
        }

    async def capabilities(self, account_id: int) -> dict[str, object]:
        await self._require_account(account_id)
        return self._capabilities(account_id).model_dump(mode="json")

    # --- вспомогательное ----------------------------------------------------

    async def _require_account(self, account_id: int) -> Account:
        account = await self._storage.get_account(account_id)
        if account is None:
            raise ServiceError(f"аккаунт {account_id} не найден")
        return account

    def _capabilities(self, account_id: int) -> Capabilities:
        return self._connections.ensure(account_id).capabilities
