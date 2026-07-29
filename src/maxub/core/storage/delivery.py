"""Жизненный цикл доставки: захват, отправка, разбор исхода.

Отделено от постановки в очередь намеренно. Постановка — это про то, что
сообщение вообще нужно отправить, и её видит прикладной код. Здесь же живёт
единственное по-настоящему тонкое место платформы: граница, за которой исход
отправки перестаёт быть заведомо известным. Все переходы состояний написаны
так, чтобы эту границу нельзя было пересечь незаметно.
"""

from __future__ import annotations

from datetime import datetime

from maxub.core.models import Event, OutboxItem, OutboxState, utcnow
from maxub.core.storage.events import EventsMixin
from maxub.core.storage.outbox import outbox_row


class DeliveryMixin(EventsMixin):
    """Переходы состояний доставки.

    Наследуется от журнала событий не ради удобства: закрытие отправки и запись
    события об этом обязаны попадать в одну транзакцию, а значит должны уметь
    писать в обе таблицы, не фиксируя изменения по отдельности.
    """

    async def claim_queued(self, limit: int = 10) -> list[OutboxItem]:
        """Атомарно забирает пачку сообщений в работу.

        Выборка и перевод в ``claimed`` выполняются одним оператором: иначе два
        воркера успели бы прочитать одни и те же строки и отправить сообщение
        дважды.

        Захват — ещё не отправка: транспорт эти записи не видел, и вернуть их в
        очередь можно без всякой сверки. Попытка засчитывается уже здесь —
        иначе повторный захват после падения процесса ничем не ограничен.
        """
        now = utcnow().isoformat()
        async with self.write() as db:
            async with db.execute(
                "UPDATE outbox SET state = ?, attempts = attempts + 1, claimed_at = ?"
                " WHERE id IN ("
                "   SELECT id FROM outbox"
                "   WHERE state = ? AND (next_attempt_at IS NULL OR next_attempt_at <= ?)"
                "   ORDER BY id LIMIT ?"
                " ) RETURNING *",
                (OutboxState.CLAIMED.value, now, OutboxState.QUEUED.value, now, limit),
            ) as cursor:
                rows = await cursor.fetchall()
        return [outbox_row(row) for row in rows]

    async def mark_sending(self, item_id: int) -> bool:
        """Отмечает начало сетевого вызова — по одной записи, перед отправкой.

        Ровно с этого момента исход перестаёт быть заведомо известным. Переход
        разрешён только из ``claimed`` и подтверждается ответом: ``False``
        означает, что запись уже не принадлежит вызывающему (её вернули в
        очередь или закрыли), и отправлять её он не вправе — иначе получатель
        получит сообщение дважды.
        """
        async with self.write() as db:
            cursor = await db.execute(
                "UPDATE outbox SET state = ? WHERE id = ? AND state = ?",
                (OutboxState.SENDING.value, item_id, OutboxState.CLAIMED.value),
            )
        return cursor.rowcount == 1

    async def release_claimed(self, item_id: int) -> None:
        """Возвращает в очередь запись, которую так и не передали транспорту.

        Срок следующей попытки не сбрасывается: он уже наступил, раз запись
        была захвачена, а обнуление сдвинуло бы порядок разбора очереди.
        """
        async with self.write() as db:
            await db.execute(
                "UPDATE outbox SET state = ? WHERE id = ? AND state = ?",
                (OutboxState.QUEUED.value, item_id, OutboxState.CLAIMED.value),
            )

    async def release_all_claimed(self) -> int:
        """Возвращает в очередь всё, что осталось захваченным после падения.

        Вызывается на старте, пока воркер ещё не работает. Без этого захваченные
        записи не разбирал бы никто: в живом потоке они уже никому не
        принадлежат, а сверять их незачем — до транспорта они не дошли.
        """
        async with self.write() as db:
            cursor = await db.execute(
                "UPDATE outbox SET state = ? WHERE state = ?",
                (OutboxState.QUEUED.value, OutboxState.CLAIMED.value),
            )
        return cursor.rowcount

    async def list_stale_sending(self) -> list[OutboxItem]:
        """Записи, застрявшие в ``sending`` после падения процесса.

        Между вызовом транспорта и записью результата процесс мог умереть,
        поэтому исход таких сообщений неизвестен. Решение принимает ядро:
        сверить с сервером, если транспорт это умеет, иначе — отдать человеку.
        Захваченные, но не отправленные записи сюда не попадают — им хватает
        [release_all_claimed][maxub.core.storage.delivery.DeliveryMixin.release_all_claimed].
        """
        async with self.db.execute(
            "SELECT * FROM outbox WHERE state = ? ORDER BY id", (OutboxState.SENDING.value,)
        ) as cursor:
            rows = await cursor.fetchall()
        return [outbox_row(row) for row in rows]

    async def schedule_retry(self, item_id: int, next_attempt_at: datetime) -> None:
        """Возвращает сообщение в очередь не раньше указанного момента.

        Из завершённых состояний запись не воскрешается: запоздавшая сверка
        иначе отправила бы заново то, что уже доставлено. Ручной повтор
        отказавшего сообщения — это
        [requeue][maxub.core.storage.delivery.DeliveryMixin.requeue], отдельное
        осознанное решение человека.
        """
        async with self.write() as db:
            await db.execute(
                "UPDATE outbox SET state = ?, next_attempt_at = ? WHERE id = ? AND state IN (?, ?)",
                (
                    OutboxState.QUEUED.value,
                    next_attempt_at.isoformat(),
                    item_id,
                    OutboxState.CLAIMED.value,
                    OutboxState.SENDING.value,
                ),
            )

    async def mark_sent_with_event(
        self, item_id: int, remote_message_id: str, event: Event
    ) -> bool:
        """Закрывает отправку и пишет событие о ней одной транзакцией.

        Раздельная запись оставляла окно: процесс, упавший между ними, оставлял
        сообщение отправленным навсегда, но без события — подписчик о нём уже
        никогда бы не узнал, а восстановить пропажу неоткуда. Одной транзакции
        для этого мало: между двумя операторами есть ``await``, и чужая
        фиксация закрыла бы её посередине, поэтому запись идёт под общим замком
        [write][maxub.core.storage.base.Database.write].

        Закрыть можно только запись «в полёте»: если её состояние успели
        изменить, событие не пишется вовсе — сообщать об отправке того, чего мы
        больше не касаемся, значит врать подписчику.

        Возвращает признак «есть что публиковать»: одно и то же сообщение может
        закрыться и обычной отправкой, и сверкой, а повтор подписчику не нужен.
        """
        return await self._close_as_sent(item_id, remote_message_id, event, OutboxState.SENDING)

    async def _close_as_sent(
        self, item_id: int, remote_message_id: str, event: Event, from_state: OutboxState
    ) -> bool:
        async with self.write() as db:
            cursor = await db.execute(
                "UPDATE outbox SET state = ?, remote_message_id = ?, sent_at = ?, error = NULL"
                " WHERE id = ? AND state = ?",
                (
                    OutboxState.SENT.value,
                    remote_message_id,
                    utcnow().isoformat(),
                    item_id,
                    from_state.value,
                ),
            )
            if cursor.rowcount != 1:
                return False
            return await self.insert_event(event)

    async def mark_failed(self, item_id: int, error: str) -> None:
        async with self.write() as db:
            await db.execute(
                "UPDATE outbox SET state = ?, error = ? WHERE id = ?",
                (OutboxState.FAILED.value, error, item_id),
            )
