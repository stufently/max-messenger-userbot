"""Отправка сообщений из очереди.

Здесь живёт вся политика повторов. Главное правило: повторяется только то, про
что достоверно известно, что оно не выполнено. Неизвестный исход повторять
нельзя — получатель увидит дубль; такие записи уходят на сверку в
[reconcile][maxub.core.reconcile].
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from datetime import timedelta

from maxub.config import Settings
from maxub.core.models import OutboxItem, utcnow
from maxub.core.ports import EventPublisher, OutboxRepository
from maxub.core.ratelimit import RateLimiter
from maxub.core.reconcile import Reconciler, sent_event
from maxub.transport.base import (
    Transport,
    TransportAuthError,
    TransportNotApplied,
    TransportRateLimited,
)

log = logging.getLogger(__name__)

IDLE_SECONDS = 0.5
SEND_ACTION = "send_text"


class OutboxWorker:
    """Разбирает очередь исходящих сообщений."""

    def __init__(
        self,
        repo: OutboxRepository,
        limiter: RateLimiter,
        get_transport: Callable[[int], Transport | None],
        settings: Settings,
        publish: EventPublisher,
        on_auth_lost: Callable[[int, str], Awaitable[None]],
    ) -> None:
        self._repo = repo
        self._limiter = limiter
        self._get_transport = get_transport
        self._settings = settings
        self._publish = publish
        self._on_auth_lost = on_auth_lost
        self._reconciler = Reconciler(repo, get_transport, publish)
        self._stopping = asyncio.Event()

    def stop(self) -> None:
        self._stopping.set()

    async def reconcile_stale(self) -> None:
        """Разбор очереди после падения процесса. Вызывается на старте демона.

        Захваченные записи возвращаются в очередь без вопросов — транспорт их
        не видел. Сверяется только то, что успело уйти в сеть, и решение о
        повторе принимается здесь же: политика повторов одна на все пути.
        """
        await self._reconciler.release_stale_claims()
        for item in await self._repo.list_stale_sending():
            if await self._reconciler.resolve(item):
                # Ждёт оно с прошлого запуска, поэтому без задержки. Лимит
                # попыток при этом соблюдается: падать в цикле можно вечно.
                await self._retry_or_fail(item, "сверка показала, что сообщение не дошло", now=True)

    # --- основной цикл ------------------------------------------------------

    async def run(self) -> None:
        while not self._stopping.is_set():
            try:
                items = await self._repo.claim_queued()
                if not items:
                    await asyncio.sleep(IDLE_SECONDS)
                    continue
                for item in items:
                    if self._stopping.is_set():
                        # Остаток пачки транспорт не увидит: возвращаем его в
                        # очередь сразу, чтобы остановка не задерживала отправку
                        # до следующего запуска.
                        await self._repo.release_claimed(item.id)
                        continue
                    await self._process(item)
            except asyncio.CancelledError:
                # Захваченный остаток пачки остаётся в claimed: разбирать его
                # уже отменённой задачей ненадёжно, это делает sweep на старте.
                raise
            except Exception:
                # Воркер обслуживает все аккаунты: необработанная ошибка
                # остановила бы отправку целиком.
                log.exception("сбой в цикле отправки")
                await asyncio.sleep(IDLE_SECONDS)

    async def _process(self, item: OutboxItem) -> None:
        """Следит, чтобы захваченная запись не зависла из-за сбоя вне транспорта.

        Ошибка лимитера или хранилища оставила бы запись в claimed до
        перезапуска, хотя отправить её никто не пробовал. Возврат в очередь
        безопасен: сработает он только пока запись всё ещё claimed.
        """
        try:
            await self._send_one(item)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("сбой при обработке сообщения %s", item.id)
            await self._repo.release_claimed(item.id)

    async def _send_one(self, item: OutboxItem) -> None:
        if item.attempts > self._settings.max_send_attempts:
            # Сюда приводят только захваты, оборвавшиеся вместе с процессом:
            # обычный путь закрывает запись раньше, в _retry_or_fail. Без этой
            # проверки сообщение, роняющее демон, забирали бы вечно.
            await self._repo.mark_failed(
                item.id,
                f"исчерпан лимит попыток ({self._settings.max_send_attempts}):"
                " отправка обрывалась вместе с процессом",
            )
            return
        transport = self._get_transport(item.account_id)
        if transport is None:
            await self._retry_or_fail(item, "нет активного соединения")
            return
        await self._limiter.acquire(item.account_id, SEND_ACTION)
        # Отметка ставится вплотную к сетевому вызову: всё, что до неё, точно
        # не дошло до сервера и повторяется без сверки.
        if not await self._repo.mark_sending(item.id):
            # Запись увели, пока мы ждали лимитер. Отправлять её теперь нельзя:
            # ею распоряжается кто-то другой, и вторая отправка дала бы дубль.
            log.warning("сообщение %s забрали до отправки, пропускаем", item.id)
            return
        try:
            remote_id = await transport.send_text(item.chat_id, item.text, item.idempotency_key)
        except TransportRateLimited as exc:
            if exc.retry_after:
                until = self._limiter.penalize(item.account_id, SEND_ACTION, exc.retry_after)
                await self._repo.save_penalty(item.account_id, SEND_ACTION, until)
            await self._retry_or_fail(item, str(exc))
            return
        except TransportNotApplied as exc:
            await self._retry_or_fail(item, str(exc))
            return
        except TransportAuthError as exc:
            await self._repo.mark_failed(item.id, str(exc))
            await self._on_auth_lost(item.account_id, str(exc))
            return
        except Exception as exc:
            # Таймаут, обрыв, TransportOutcomeUnknown: сообщение могло уйти.
            # Ни отказ, ни повтор тут не годятся — сначала спрашиваем сервер.
            await self._unknown_outcome(item, exc)
            return
        await self._repo.clear_penalty(item.account_id, SEND_ACTION)
        # Отметка об отправке и событие о ней пишутся вместе: иначе падение
        # между ними оставило бы сообщение отправленным, но никем не увиденным.
        event = sent_event(item, remote_id)
        if await self._repo.mark_sent_with_event(item.id, remote_id, event):
            self._publish(event)

    async def _unknown_outcome(self, item: OutboxItem, exc: Exception) -> None:
        """Сводит неоднозначную отправку с сервером, не выходя из цикла.

        Запись остаётся в sending: если сверка не удастся или процесс упадёт
        прямо сейчас, её подберёт sweep на следующем запуске.
        """
        log.warning("исход отправки %s неизвестен: %s", item.id, exc)
        if await self._reconciler.resolve(item):
            # Сервер подтвердил, что сообщения нет. Повтор — по общим правилам,
            # с задержкой и с оглядкой на лимит попыток.
            await self._retry_or_fail(item, str(exc))

    async def _retry_or_fail(self, item: OutboxItem, error: str, *, now: bool = False) -> None:
        """Назначает повтор или закрывает запись, если попытки исчерпаны.

        ``now`` снимает задержку — она нужна против частых обращений к серверу,
        а сообщение, пролежавшее до перезапуска, и так уже подождало.
        """
        if item.attempts >= self._settings.max_send_attempts:
            await self._repo.mark_failed(
                item.id, f"исчерпан лимит попыток ({self._settings.max_send_attempts}): {error}"
            )
            return
        delay = (
            timedelta()
            if now
            else backoff_delay(
                attempt=item.attempts,
                base=self._settings.retry_base_seconds,
                maximum=self._settings.retry_max_seconds,
            )
        )
        await self._repo.schedule_retry(item.id, utcnow() + delay)


def backoff_delay(attempt: int, base: float, maximum: float) -> timedelta:
    """Экспоненциальная задержка со случайным разбросом.

    Разброс нужен, чтобы после общего сбоя все аккаунты не пошли на сервер
    одновременно.
    """
    raw = min(base * (2 ** max(0, attempt - 1)), maximum)
    return timedelta(seconds=raw * random.uniform(0.5, 1.0))
