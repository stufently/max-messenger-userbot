"""Соединения аккаунтов: подключение, надзор, переподключение.

Внутренний API MAX рвёт соединение без предупреждения, поэтому за каждым
аккаунтом следит отдельный надзорный цикл — его политика повторов живёт в
[supervisor][maxub.core.supervisor]. Порядок при подключении обратный
интуитивному: сначала открывается живой поток и только потом добирается
пропущенное — за это отвечает [stream][maxub.core.stream]. Готовность (`READY`)
по-прежнему объявляется лишь после добора: до него обработчики работали бы на
устаревшем состоянии — и только если подписка при этом жива.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from functools import partial

from maxub.config import Settings
from maxub.core.backfill import Backfiller
from maxub.core.events import account_state_event
from maxub.core.models import AccountState, Event, Session
from maxub.core.ports import AccountRepository, EventPublisher, EventSink, TransportFactory
from maxub.core.stream import open_stream
from maxub.core.supervisor import ConnectionSupervisor, SupervisorRegistry
from maxub.transport.base import Transport, TransportAuthError

log = logging.getLogger(__name__)


class ConnectionManager:
    """Владеет транспортами аккаунтов и их жизненным циклом."""

    def __init__(
        self,
        repo: AccountRepository,
        factory: TransportFactory,
        settings: Settings,
        emit: EventSink,
        publish: EventPublisher,
    ) -> None:
        self._repo = repo
        self._factory = factory
        self._settings = settings
        self._emit = emit
        # Раздача без записи: событие о смене состояния уже попало в журнал той
        # же транзакцией, что и само состояние, и записывать его второй раз
        # значит спорить с уникальным индексом на ровном месте.
        self._publish = publish
        self._backfill = Backfiller(repo, emit)
        self._transports: dict[int, Transport] = {}
        # Аккаунты, чей транспорт прямо сейчас годен для работы. Держится
        # отдельно от самого словаря транспортов, потому что обрыв связи их
        # оттуда не убирает: надзор закрывает прежний транспорт только в момент
        # переподключения, а до тех пор — всю паузу backoff, до пяти минут —
        # объект остаётся на месте и выглядит рабочим.
        self._live: set[int] = set()
        # Последняя известная сессия аккаунта. Держится отдельно от той, что
        # запомнил надзор: сервер может выдать новую при любом подключении.
        self._sessions: dict[int, Session] = {}
        self._supervisors = SupervisorRegistry()

    # --- доступ -------------------------------------------------------------

    def get(self, account_id: int) -> Transport | None:
        """Транспорт аккаунта как объект — независимо от того, работает ли он.

        Нужен входу: пока идёт вход, соединения в рабочем смысле ещё нет, а
        разговаривать с сервером уже надо. Всем, кто собирается что-то
        отправлять, нужен не этот метод, а
        [live][maxub.core.sync.ConnectionManager.live].
        """
        return self._transports.get(account_id)

    def live(self, account_id: int) -> Transport | None:
        """Транспорт, которым прямо сейчас можно пользоваться.

        Мёртвое соединение наружу не отдаётся, и это не косметика. Обрыв не
        убирает транспорт из словаря: прежний объект закрывается только в
        момент переподключения, а до тех пор — всю паузу backoff, до
        `reconnect_max_seconds` — остаётся на месте и выглядит рабочим.
        Отправка, получив такой объект, шла в закрытый сокет, считала отказ за
        неудачную попытку и тратила лимит: пять кругов повторов укладываются в
        минуту, и сообщение умирало, ни разу не побывав в сети, — при том что
        соединение всё это время поднималось. Отсутствие транспорта отправка
        понимает верно: ждёт, не тратя попыток.
        """
        if account_id not in self._live:
            return None
        return self._transports.get(account_id)

    def ensure(self, account_id: int) -> Transport:
        """Возвращает транспорт аккаунта, создавая его при необходимости."""
        transport = self._transports.get(account_id)
        if transport is None:
            transport = self._factory()
            self._transports[account_id] = transport
        return transport

    @property
    def active(self) -> int:
        return self._supervisors.active

    # --- подключение --------------------------------------------------------

    async def connect(self, account_id: int, session: Session) -> Session:
        """Подключает аккаунт, доводит до готовности и оставляет под надзором.

        Возвращает сессию, с которой аккаунт действительно работает: сервер мог
        выдать новую прямо на этом подключении. Вызывающему она нужна, чтобы
        сохранить именно её — записав ту, что передал, он затёр бы обновлённую.

        Ошибку наверх пробрасывает намеренно: вызывающий решает, что делать с
        неудачей. Надзор при этом не заводится — для этого есть
        [supervise][maxub.core.sync.ConnectionManager.supervise].
        """
        # Прежний надзор снимается до нового подключения, а не после: иначе на
        # аккаунте недолго живут два живых потока, наперегонки пишущих курсор.
        await self._supervisors.stop(account_id)
        transport = self.ensure(account_id)
        await self.set_state(account_id, AccountState.CONNECTING)
        # Разбор неудачи целиком здесь, и это не мелочь оформления. Раньше часть
        # ошибок классифицировал вызывающий, и на старте демона аккаунт получал
        # `BACKOFF` дважды: сначала отсюда, потом от того, кто поймал исключение.
        # В журнале это два события об одном переходе.
        try:
            session = await self._remember(account_id, session, await transport.connect(session))
            # Отказ может прийти и после `connect()` — например, подписка живого
            # потока упрётся в отозванную сессию.
            pump = await self._open_stream(account_id, transport)
        except TransportAuthError as exc:
            # `BACKOFF` тут обещал бы, что повтор поможет, а помогает только
            # новый вход.
            await self.set_state(account_id, AccountState.AUTH_REQUIRED, str(exc))
            raise
        except Exception as exc:
            # Иначе аккаунт остался бы в connecting или syncing, хотя ни то, ни
            # другое уже не происходит.
            await self.set_state(account_id, AccountState.BACKOFF, str(exc))
            raise
        self._start_supervisor(account_id, session, pump)
        return session

    def supervise(self, account_id: int, session: Session) -> None:
        """Заводит надзор над аккаунтом, который подключить не удалось.

        Без этого неудача первого подключения оставляла аккаунт в `BACKOFF`
        навсегда: повторять попытки было некому до перезапуска демона.
        """
        self._start_supervisor(account_id, session, None)

    async def _open_stream(self, account_id: int, transport: Transport) -> asyncio.Task[None]:
        """Доводит аккаунт до готовности: подписка, добор, состояние.

        Сам порядок и его цена описаны в
        [open_stream][maxub.core.stream.open_stream]; здесь остаётся только то,
        что знает про состояние аккаунта. Готовность объявляется лишь после
        того, как подписка подтверждена и пережила добор: `READY` при мёртвом
        потоке — это аккаунт, за которым никто не слушает сервер.
        """
        await self.set_state(account_id, AccountState.SYNCING)
        pump = await open_stream(
            account_id,
            transport,
            self._repo,
            self._emit,
            partial(self._backfill.run, account_id, transport),
        )
        await self.set_state(account_id, AccountState.READY)
        await self._emit(
            Event(
                account_id=account_id,
                kind="account.ready",
                payload={},
                dedup_key=f"ready:{account_id}:{id(transport)}",
            )
        )
        return pump

    # --- надзор -------------------------------------------------------------

    def _start_supervisor(
        self, account_id: int, session: Session, pump: asyncio.Task[None] | None
    ) -> None:
        self._supervisors.start(
            account_id,
            ConnectionSupervisor(
                account_id,
                partial(self.set_state, account_id),
                self._settings,
                partial(self._reconnect, account_id, session),
            ),
            pump,
        )

    async def _reconnect(self, account_id: int, session: Session) -> asyncio.Task[None]:
        """Поднимает соединение заново, закрывая за собой прежнее.

        Транспорт предыдущей попытки закрывается явно: просто заменить его в
        словаре — значит потерять ссылку вместе с открытым сокетом, до которого
        не доберётся даже `shutdown()`.
        """
        await self._close_transport(account_id)
        transport = self._factory()
        self._transports[account_id] = transport
        # Надзор держит ту сессию, с которой аккаунт подняли изначально, а
        # сервер мог выдать новую по дороге. Берём последнюю известную, иначе
        # переподключение раз за разом ходило бы с протухшим токеном.
        session = self._sessions.get(account_id, session)
        try:
            await self.set_state(account_id, AccountState.CONNECTING)
            await self._remember(account_id, session, await transport.connect(session))
            return await self._open_stream(account_id, transport)
        except BaseException:
            await self._close_transport(account_id)
            raise

    async def set_state(
        self, account_id: int, state: AccountState, error: str | None = None
    ) -> None:
        """Меняет состояние аккаунта и сообщает об этом подписчикам.

        Единая точка, а не пара вызовов на каждом переходе: половина переходов
        осталась бы без события при первой же правке, и подписчик узнавал бы о
        потере авторизации только опросом статуса.

        Публичная и единственная: через неё идут и переходы, случившиеся вне
        соединений, — вход, выключение аккаунта, потеря авторизации при
        отправке. Здесь же обновляется признак пригодности транспорта, и
        запись состояния в обход рассинхронизировала бы их: аккаунт числился бы
        разлогиненным, а отправка продолжала бы ходить в отозванное соединение.
        """
        # Годным транспорт считается только в `ready`. Все прочие состояния —
        # это либо ещё не поднятое соединение, либо уже не работающее.
        if state is AccountState.READY:
            self._live.add(account_id)
        else:
            self._live.discard(account_id)
        event = account_state_event(account_id, state, error)
        if event is None:
            await self._repo.set_account_state(account_id, state, error)
            return
        if await self._repo.set_account_state_with_event(account_id, state, error, event):
            self._publish(event)

    async def _remember(
        self, account_id: int, current: Session, refreshed: Session | None
    ) -> Session:
        """Запоминает сессию аккаунта, сохраняя обновлённую сервером.

        Транспорт возвращает новую сессию только тогда, когда сервер её выдал.
        Записать её надо сразу: следующий запуск демона возьмёт сессию из базы,
        и старый токен там означал бы просьбу войти заново — при том что вход
        только что прошёл успешно.
        """
        session = refreshed if refreshed is not None else current
        self._sessions[account_id] = session
        if refreshed is not None:
            await self._repo.save_session(account_id, session.model_dump(mode="json"))
        return session

    # --- остановка ----------------------------------------------------------

    async def _close_transport(self, account_id: int) -> None:
        self._live.discard(account_id)
        transport = self._transports.pop(account_id, None)
        if transport is not None:
            with contextlib.suppress(Exception):
                await transport.disconnect()

    async def disconnect(self, account_id: int) -> None:
        await self._supervisors.stop(account_id)
        await self._close_transport(account_id)
        self._sessions.pop(account_id, None)

    async def shutdown(self) -> None:
        for account_id in self._supervisors.accounts():
            await self.disconnect(account_id)
        for transport in self._transports.values():
            with contextlib.suppress(Exception):
                await transport.disconnect()
        self._transports.clear()
        self._live.clear()
