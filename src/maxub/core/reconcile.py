"""Сверка отправок с неизвестным исходом.

Вынесено из [sender][maxub.core.sender], потому что это отдельная политика:
там решают, когда повторять, здесь — есть ли вообще что повторять. Правило
одно: повтор допустим, только когда сервер доказал, что сообщения нет. «Не
нашли» и «не смогли выяснить» — разные ответы, и путать их нельзя: во втором
случае повтор придёт получателю вторым сообщением.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from maxub.core.models import Event, OutboxItem
from maxub.core.ports import EventPublisher, OutboxRepository
from maxub.transport.base import ReconcileOutcome, Transport

log = logging.getLogger(__name__)

UNKNOWN_OUTCOME = "исход отправки неизвестен, требуется решение вручную"


def sent_event(item: OutboxItem, remote_id: str) -> Event:
    """Событие об отправке. Ключ дедупликации — клиентский токен сообщения.

    Одно и то же сообщение может закрыться и обычной отправкой, и сверкой;
    подписчик обязан увидеть его один раз.
    """
    return Event(
        account_id=item.account_id,
        kind="message.sent",
        payload={"chat_id": item.chat_id, "remote_message_id": remote_id},
        dedup_key=f"sent:{item.idempotency_key}",
    )


class Reconciler:
    """Выясняет у сервера судьбу сообщений, застрявших в состоянии «в полёте»."""

    def __init__(
        self,
        repo: OutboxRepository,
        get_transport: Callable[[int], Transport | None],
        publish: EventPublisher,
    ) -> None:
        self._repo = repo
        self._get_transport = get_transport
        self._publish = publish

    async def resolve(self, item: OutboxItem) -> bool:
        """Разбирает одну запись в состоянии ``sending``.

        Возвращает ``True`` только тогда, когда сервер доказал, что сообщения у
        него нет, — решение о повторе принимает вызывающий, у него своя
        политика задержек. Во всех прочих исходах запись закрывается здесь:
        доставлена или отдана человеку.
        """
        transport = self._get_transport(item.account_id)
        if transport is None:
            # Причины различаются: соединение может вернуться, а неумение
            # транспорта — нет. Человеку, который будет разбирать запись, это
            # говорит разное.
            await self._fail(item, "сверять нечем: нет активного соединения")
            return False
        if not transport.capabilities.reconcile:
            await self._fail(item, "транспорт не умеет сверять отправленное")
            return False
        try:
            result = await transport.reconcile_send(item.chat_id, item.idempotency_key)
        except Exception as exc:
            # Неудачная сверка — это тот же неизвестный исход, а не отказ:
            # повторять по ней нельзя.
            log.warning("не удалось сверить сообщение %s: %s", item.id, exc)
            await self._fail(item, f"сверка не удалась ({exc})")
            return False

        if result.outcome is ReconcileOutcome.NOT_FOUND:
            return True
        if result.outcome is ReconcileOutcome.FOUND:
            if result.message is None:
                # Нарушение контракта транспорта: «нашли» без сообщения. Ставить
                # выдуманный идентификатор нельзя, значит исход неизвестен.
                await self._fail(item, "транспорт сообщил «найдено» без самого сообщения")
                return False
            event = sent_event(item, result.message.remote_id)
            if await self._repo.mark_sent_with_event(item.id, result.message.remote_id, event):
                self._publish(event)
            return False
        await self._fail(item, result.detail or "сверка не дала ответа")
        return False

    async def release_stale_claims(self) -> None:
        """Возвращает в очередь записи, захваченные упавшим процессом.

        Сверять их не нужно и нельзя перепутать с отправленными: до транспорта
        они не дошли. Вызывается на старте, пока воркер не работает, — иначе
        отобрала бы записи у него.
        """
        released = await self._repo.release_all_claimed()
        if released:
            log.info("возвращено в очередь после перезапуска: %s", released)

    async def _fail(self, item: OutboxItem, reason: str) -> None:
        await self._repo.mark_failed(item.id, f"{UNKNOWN_OUTCOME}: {reason}")
