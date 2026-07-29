"""Надзор за соединением аккаунта.

Вынесен из [sync][maxub.core.sync] отдельно: политика повторов — это разговор
про время и счётчики, а не про владение транспортами, и вместе они читались
хуже. Здесь живёт единственный ответ на вопрос «когда пробовать снова».
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from functools import partial

from maxub.config import Settings
from maxub.core.models import AccountState
from maxub.core.sender import backoff_delay
from maxub.transport.base import TransportAuthError, TransportError

log = logging.getLogger(__name__)


class StreamEnded(TransportError):
    """Живой поток закончился сам, без ошибки.

    Для платформы это тот же обрыв: событий больше не будет. Завершать надзор
    молча нельзя — аккаунт остался бы «готовым» без соединения до перезапуска
    демона.
    """


#: Сколько соединение должно прожить, чтобы считаться восстановленным. Сам факт
#: успешного `connect()` восстановлением не считается: сервер охотно принимает
#: подключение и тут же его рвёт, и по такому признаку счётчик попыток
#: обнулялся бы вечно, оставляя задержку минимальной.
STABLE_CONNECTION_SECONDS = 30.0

#: Поднимает соединение заново и возвращает задачу живого потока.
Reopen = Callable[[], Awaitable["asyncio.Task[None]"]]

#: Записывает состояние аккаунта и рассказывает о нём подписчикам. Надзор берёт
#: именно её, а не хранилище: обрывы и потеря авторизации случаются здесь, и
#: писать состояние мимо общего пути значило бы, что живой поток о них молчит —
#: ровно о том, ради чего его и слушают.
StateWriter = Callable[[AccountState, str | None], Awaitable[None]]


class ConnectionSupervisor:
    """Держит аккаунт подключённым, пока надзор не отменят."""

    def __init__(
        self,
        account_id: int,
        set_state: StateWriter,
        settings: Settings,
        reopen: Reopen,
    ) -> None:
        self._account_id = account_id
        self._write_state = set_state
        self._settings = settings
        self._reopen = reopen

    async def run(self, pump: asyncio.Task[None] | None) -> None:
        """Ждёт обрыва живого потока и поднимает соединение заново.

        `pump` — уже работающий поток; `None` означает, что подключиться пока не
        удалось и начинать надо с паузы.
        """
        attempt = 0
        started_at = asyncio.get_running_loop().time()
        try:
            while True:
                if pump is None:
                    attempt += 1
                    await self._pause(attempt)
                    try:
                        pump = await self._reopen()
                    except TransportAuthError as exc:
                        await self._auth_lost(exc)
                        return
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        log.warning("подключение аккаунта %s не удалось: %s", self._account_id, exc)
                        await self._set_state(AccountState.BACKOFF, exc)
                        continue
                    started_at = asyncio.get_running_loop().time()
                    continue
                try:
                    await pump
                    # Итератор кончился сам — это обрыв, а не повод разойтись.
                    raise StreamEnded("живой поток завершился без ошибки")
                except asyncio.CancelledError:
                    raise
                except TransportAuthError as exc:
                    await self._auth_lost(exc)
                    return
                except Exception as exc:
                    attempt = self._next_attempt(attempt, started_at, exc)
                    await self._set_state(AccountState.BACKOFF, exc)
                    pump = None
        finally:
            # Надзор могли отменить снаружи (disconnect, остановка демона) —
            # живой поток не должен пережить своего надзирателя. Дожидаемся
            # его здесь же: иначе отменённый поток успевал записать курсор уже
            # после того, как вызвавший disconnect закрыл транспорт и базу.
            if pump is not None:
                pump.cancel()
                with contextlib.suppress(Exception, asyncio.CancelledError):
                    await pump

    def _next_attempt(self, attempt: int, started_at: float, exc: Exception) -> int:
        """Решает, продолжать ли экспоненту после обрыва.

        Счётчик обнуляется только после соединения, которое действительно
        поработало: раньше он сбрасывался при любом успешном `connect()`, и
        аккаунт с мгновенно рвущимся соединением ломился на сервер с
        минимальной задержкой без конца.
        """
        lived = asyncio.get_running_loop().time() - started_at
        log.warning(
            "соединение аккаунта %s прервано через %.1f с: %s", self._account_id, lived, exc
        )
        return 0 if lived >= STABLE_CONNECTION_SECONDS else attempt

    async def _pause(self, attempt: int) -> None:
        delay = backoff_delay(
            attempt=attempt,
            base=self._settings.reconnect_base_seconds,
            maximum=self._settings.reconnect_max_seconds,
        )
        log.info(
            "аккаунт %s: попытка подключения %s через %.1f с",
            self._account_id,
            attempt,
            delay.total_seconds(),
        )
        await asyncio.sleep(delay.total_seconds())

    async def _set_state(self, state: AccountState, exc: Exception) -> None:
        await self._write_state(state, str(exc))

    async def _auth_lost(self, exc: Exception) -> None:
        """Сессия отозвана: повторять бессмысленно, нужен новый вход."""
        await self._set_state(AccountState.AUTH_REQUIRED, exc)


class SupervisorRegistry:
    """Надзорные задачи по аккаунтам: по одной на аккаунт."""

    def __init__(self) -> None:
        self._tasks: dict[int, asyncio.Task[None]] = {}

    @property
    def active(self) -> int:
        return len(self._tasks)

    def accounts(self) -> list[int]:
        return list(self._tasks)

    def start(
        self,
        account_id: int,
        supervisor: ConnectionSupervisor,
        pump: asyncio.Task[None] | None,
    ) -> None:
        task = asyncio.create_task(supervisor.run(pump))
        self._tasks[account_id] = task
        # Завершившийся надзор убирается сам: иначе счётчик активных считал бы
        # аккаунты, за которыми уже никто не следит.
        task.add_done_callback(partial(self._forget, account_id))

    async def stop(self, account_id: int) -> None:
        """Снимает надзор и дожидается его — вместе с живым потоком."""
        task = self._tasks.pop(account_id, None)
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    def _forget(self, account_id: int, task: asyncio.Task[None]) -> None:
        if self._tasks.get(account_id) is task:
            del self._tasks[account_id]
