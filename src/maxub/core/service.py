"""Ядро: сервисы приложения.

Не зависит ни от FastAPI, ни от Typer, ни от моделей конкретной транспортной
библиотеки — они остаются адаптерами по краям.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging

from maxub.config import Settings
from maxub.core.models import Account, AccountState, Event, OutboxItem, Session, utcnow
from maxub.core.ratelimit import RateLimiter
from maxub.core.storage import DuplicateAccountError, Storage
from maxub.transport.base import (
    Capabilities,
    Transport,
    TransportAuthError,
    TransportNotApplied,
    TransportRateLimited,
    TransportUnsupported,
)

log = logging.getLogger(__name__)

WORKER_IDLE_SECONDS = 0.5
MAX_SEND_ATTEMPTS = 5
LISTENER_QUEUE_SIZE = 1000
DEDUP_WINDOW_SECONDS = 60.0

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


def idempotency_key(account_id: int, chat_id: str, text: str, nonce: str | None) -> str:
    """Ключ идемпотентности.

    Без явного nonce два одинаковых сообщения в один чат считаются повтором —
    это защищает от дублей при ретраях клиента.
    """
    raw = f"{account_id}|{chat_id}|{text}|{nonce or ''}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class UserbotService:
    def __init__(
        self,
        settings: Settings,
        storage: Storage,
        transport_factory: object,
    ) -> None:
        self._settings = settings
        self._storage = storage
        self._transport_factory = transport_factory
        self._transports: dict[int, Transport] = {}
        self._challenges: dict[str, int] = {}
        self._limiter = RateLimiter(
            rate_per_minute=settings.send_rate_per_minute,
            burst=settings.send_burst,
            jitter_seconds=settings.send_jitter_seconds,
        )
        self._worker: asyncio.Task[None] | None = None
        self._pumps: dict[int, asyncio.Task[None]] = {}
        self._listeners: list[asyncio.Queue[Event]] = []
        self._stopping = asyncio.Event()

    # --- жизненный цикл -----------------------------------------------------

    async def start(self) -> None:
        await self._storage.open()
        stale = await self._storage.recover_stale_sending()
        for item in stale:
            log.warning(
                "сообщение %s осталось в состоянии отправки после рестарта,"
                " требуется решение вручную",
                item.id,
            )
        for account in await self._storage.list_accounts():
            # Восстанавливаем всё, что не остановлено вручную: аккаунт мог
            # застрять в connecting или backoff из-за падения процесса.
            if account.state in RESUMABLE_STATES:
                await self._storage.set_account_state(account.id, AccountState.CONNECTING)
                try:
                    await self._reconnect(account)
                except Exception as exc:
                    log.warning("не удалось переподключить аккаунт %s: %s", account.id, exc)
                    await self._storage.set_account_state(
                        account.id, AccountState.BACKOFF, str(exc)
                    )
        self._worker = asyncio.create_task(self._drain_outbox())

    async def stop(self) -> None:
        self._stopping.set()
        tasks = [self._worker, *self._pumps.values()]
        for task in tasks:
            if task is not None:
                task.cancel()
        for task in tasks:
            if task is not None:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._pumps.clear()
        for transport in self._transports.values():
            with contextlib.suppress(Exception):
                await transport.disconnect()
        self._transports.clear()
        await self._storage.close()

    # --- аккаунты -----------------------------------------------------------

    async def add_account(self, phone: str, label: str | None = None) -> Account:
        try:
            return await self._storage.add_account(phone, label)
        except DuplicateAccountError as exc:
            raise ServiceError(str(exc)) from exc

    async def list_accounts(self) -> list[Account]:
        return await self._storage.list_accounts()

    async def start_login(self, account_id: int) -> str:
        account = await self._require_account(account_id)
        transport = self._transport_for(account_id)
        challenge = await transport.start_login(account.phone)
        self._challenges[challenge.challenge_id] = account_id
        await self._storage.set_account_state(account_id, AccountState.AUTH_REQUIRED)
        return challenge.challenge_id

    async def complete_login(self, challenge_id: str, code: str) -> Account:
        account_id = self._challenges.get(challenge_id)
        if account_id is None:
            raise ServiceError("неизвестный challenge_id")
        transport = self._transport_for(account_id)
        try:
            session = await transport.complete_login(challenge_id, code, account_id)
        except TransportAuthError as exc:
            await self._storage.set_account_state(account_id, AccountState.AUTH_REQUIRED, str(exc))
            raise ServiceError(str(exc)) from exc
        del self._challenges[challenge_id]
        await self._storage.save_session(account_id, session.model_dump(mode="json"))
        account = await self._require_account(account_id)
        await self._connect(account, session, transport)
        return await self._require_account(account_id)

    async def _reconnect(self, account: Account) -> None:
        payload = await self._storage.load_session(account.id)
        if payload is None:
            await self._storage.set_account_state(account.id, AccountState.AUTH_REQUIRED)
            return
        transport = self._transport_for(account.id)
        await self._connect(account, Session.model_validate(payload), transport)

    async def _connect(self, account: Account, session: Session, transport: Transport) -> None:
        await self._storage.set_account_state(account.id, AccountState.CONNECTING)
        try:
            await transport.connect(session)
        except TransportAuthError as exc:
            await self._storage.set_account_state(account.id, AccountState.AUTH_REQUIRED, str(exc))
            raise ServiceError(str(exc)) from exc
        # Ресинк выполняется до запуска потока событий, чтобы обработчики не
        # начинали работу на устаревшем состоянии.
        await self._storage.set_account_state(account.id, AccountState.SYNCING)
        if await self._storage.load_cursor(account.id) is None:
            await self._storage.save_cursor(account.id, utcnow().isoformat())
        self._start_pump(account.id, transport)
        await self._storage.set_account_state(account.id, AccountState.READY)
        await self._emit(
            Event(
                account_id=account.id,
                kind="account.ready",
                payload={"phone": account.phone},
                dedup_key=f"ready:{account.id}:{utcnow().isoformat()}",
            )
        )

    def _start_pump(self, account_id: int, transport: Transport) -> None:
        existing = self._pumps.pop(account_id, None)
        if existing is not None:
            existing.cancel()
        self._pumps[account_id] = asyncio.create_task(self._pump_events(account_id, transport))

    async def disable_account(self, account_id: int, reason: str) -> Account:
        await self._require_account(account_id)
        pump = self._pumps.pop(account_id, None)
        if pump is not None:
            pump.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pump
        transport = self._transports.pop(account_id, None)
        if transport is not None:
            with contextlib.suppress(Exception):
                await transport.disconnect()
        await self._storage.set_account_state(account_id, AccountState.DISABLED, reason)
        return await self._require_account(account_id)

    # --- отправка -----------------------------------------------------------

    async def enqueue_message(
        self, account_id: int, chat_id: str, text: str, nonce: str | None = None
    ) -> tuple[OutboxItem, bool]:
        account = await self._require_account(account_id)
        if account.state is not AccountState.READY:
            raise ServiceError(f"аккаунт в состоянии {account.state.value}, отправка недоступна")
        capabilities = self._capabilities(account_id)
        if not capabilities.send_text:
            raise TransportUnsupported("транспорт не умеет отправлять текст")
        key = idempotency_key(account_id, chat_id, text, nonce)
        # С явным nonce дедупликация действует бессрочно: клиент сам управляет
        # идемпотентностью. Без него — только в пределах окна, иначе повторно
        # отправить тот же текст стало бы невозможно навсегда.
        window = float("inf") if nonce else DEDUP_WINDOW_SECONDS
        return await self._storage.enqueue(account_id, chat_id, text, key, window)

    async def fetch_history(
        self, account_id: int, chat_id: str, limit: int
    ) -> list[dict[str, object]]:
        await self._require_account(account_id)
        if not self._capabilities(account_id).fetch_history:
            raise TransportUnsupported("транспорт не умеет выгружать историю")
        transport = self._transport_for(account_id)
        messages = await transport.fetch_history(chat_id, limit)
        return [m.model_dump(mode="json") for m in messages]

    async def _drain_outbox(self) -> None:
        while not self._stopping.is_set():
            try:
                items = await self._storage.claim_queued()
                if not items:
                    await asyncio.sleep(WORKER_IDLE_SECONDS)
                    continue
                for item in items:
                    await self._send_one(item)
            except asyncio.CancelledError:
                raise
            except Exception:
                # Воркер обслуживает все аккаунты: любая необработанная ошибка
                # остановила бы отправку целиком, поэтому цикл переживает её.
                log.exception("сбой в цикле отправки")
                await asyncio.sleep(WORKER_IDLE_SECONDS)

    async def _send_one(self, item: OutboxItem) -> None:
        transport = self._transports.get(item.account_id)
        if transport is None:
            await self._storage.mark_failed(item.id, "нет активного соединения")
            return
        await self._limiter.acquire(item.account_id, "send_text")
        try:
            remote_id = await transport.send_text(item.chat_id, item.text)
        except TransportRateLimited as exc:
            if exc.retry_after:
                self._limiter.penalize(item.account_id, "send_text", exc.retry_after)
            await self._retry_or_fail(item, str(exc))
            return
        except TransportNotApplied as exc:
            # Достоверно известно, что действие не выполнено — повтор безопасен.
            await self._retry_or_fail(item, str(exc))
            return
        except TransportAuthError as exc:
            await self._storage.mark_failed(item.id, str(exc))
            await self._storage.set_account_state(
                item.account_id, AccountState.AUTH_REQUIRED, str(exc)
            )
            return
        except Exception as exc:
            # Исход неизвестен: сообщение могло уйти. Автоповтор запрещён,
            # иначе получатель увидит дубль.
            await self._storage.mark_failed(item.id, f"исход неизвестен: {exc}")
            return
        await self._storage.mark_sent(item.id, remote_id)
        await self._emit(
            Event(
                account_id=item.account_id,
                kind="message.sent",
                payload={"chat_id": item.chat_id, "remote_message_id": remote_id},
                dedup_key=f"sent:{item.idempotency_key}",
            )
        )

    async def _retry_or_fail(self, item: OutboxItem, error: str) -> None:
        """Возвращает сообщение в очередь, пока не исчерпан лимит попыток."""
        if item.attempts >= MAX_SEND_ATTEMPTS:
            await self._storage.mark_failed(
                item.id, f"исчерпан лимит попыток ({MAX_SEND_ATTEMPTS}): {error}"
            )
            return
        await self._storage.requeue(item.id)

    # --- события ------------------------------------------------------------

    async def _pump_events(self, account_id: int, transport: Transport) -> None:
        """Переносит входящие сообщения транспорта в журнал событий.

        Ключ дедупликации строится по идентификатору сообщения на стороне MAX,
        поэтому повторная выдача после переподключения не создаёт дублей.
        """
        try:
            async for message in transport.events():
                await self._emit(
                    Event(
                        account_id=account_id,
                        kind="message.received",
                        payload=message.model_dump(mode="json"),
                        dedup_key=f"recv:{account_id}:{message.remote_id}",
                    )
                )
                await self._storage.save_cursor(account_id, message.remote_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("поток событий аккаунта %s прерван", account_id)
            await self._storage.set_account_state(
                account_id, AccountState.BACKOFF, "поток событий прерван"
            )

    async def _emit(self, event: Event) -> None:
        if not await self._storage.record_event(event):
            return
        for queue in list(self._listeners):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # Медленный подписчик не должен раздувать память демона.
                log.warning("подписчик не успевает читать события, событие отброшено")

    def subscribe(self) -> asyncio.Queue[Event]:
        queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=LISTENER_QUEUE_SIZE)
        self._listeners.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[Event]) -> None:
        with contextlib.suppress(ValueError):
            self._listeners.remove(queue)

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
            "connections": len(self._transports),
            "outbox": await self._storage.outbox_stats(),
        }

    # --- вспомогательное ----------------------------------------------------

    async def _require_account(self, account_id: int) -> Account:
        account = await self._storage.get_account(account_id)
        if account is None:
            raise ServiceError(f"аккаунт {account_id} не найден")
        return account

    def _transport_for(self, account_id: int) -> Transport:
        transport = self._transports.get(account_id)
        if transport is None:
            factory = self._transport_factory
            transport = factory()  # type: ignore[operator]
            self._transports[account_id] = transport
        return transport

    def _capabilities(self, account_id: int) -> Capabilities:
        return self._transport_for(account_id).capabilities
