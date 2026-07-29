"""Постановка сообщений в очередь и чтение очереди.

Переходы состояний доставки живут отдельно, в
[delivery][maxub.core.storage.delivery]: здесь — только «сообщение нужно
отправить», там — «что с ним произошло».
"""

from __future__ import annotations

import aiosqlite

from maxub.core.models import OutboxItem, OutboxState, utcnow
from maxub.core.storage.base import Database, parse_dt

ENQUEUE_KEY_ATTEMPTS = 5


def outbox_row(row: aiosqlite.Row) -> OutboxItem:
    """Строка таблицы в доменную модель.

    Вынесено на уровень модуля, а не в примесь: разбор доставки читает те же
    строки, и дублировать отображение в двух местах — верный способ разойтись
    при следующем изменении схемы.
    """
    return OutboxItem(
        id=row["id"],
        account_id=row["account_id"],
        chat_id=row["chat_id"],
        text=row["text"],
        idempotency_key=row["idempotency_key"],
        state=OutboxState(row["state"]),
        attempts=row["attempts"],
        remote_message_id=row["remote_message_id"],
        error=row["error"],
        created_at=parse_dt(row["created_at"]),
        claimed_at=parse_dt(row["claimed_at"]) if row["claimed_at"] else None,
        next_attempt_at=parse_dt(row["next_attempt_at"]) if row["next_attempt_at"] else None,
        sent_at=parse_dt(row["sent_at"]) if row["sent_at"] else None,
    )


class OutboxMixin(Database):
    async def enqueue(
        self,
        account_id: int,
        chat_id: str,
        text: str,
        idempotency_key: str,
        dedup_window_seconds: float,
    ) -> tuple[OutboxItem, bool]:
        """Ставит сообщение в очередь. Второй элемент — признак «поставлено».

        Дедупликация ограничена окном: повтор в пределах окна считается
        случайным ретраем и отбрасывается, а тот же текст спустя время — это
        осмысленное повторное сообщение, и запрещать его навсегда нельзя.

        Вставка идёт через ``ON CONFLICT DO NOTHING``: две одновременные
        одинаковые заявки разрешаются уникальным индексом, а не проверкой,
        выполненной до вставки.

        Разбор коллизии ключа — вставка, чтение и вторая вставка с уточнённым
        ключом — целиком идёт под замком записи. Иначе между вставкой и
        перечитыванием строки чужая фиксация закрывала бы транзакцию, а
        соседняя попытка успевала занять тот же уточнённый ключ.
        """
        now = utcnow()
        key = idempotency_key
        async with self.write() as db:
            for attempt in range(ENQUEUE_KEY_ATTEMPTS):
                cursor = await db.execute(
                    "INSERT INTO outbox"
                    " (account_id, chat_id, text, idempotency_key, state, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?)"
                    " ON CONFLICT(idempotency_key) DO NOTHING",
                    (account_id, chat_id, text, key, OutboxState.QUEUED.value, now.isoformat()),
                )
                if cursor.rowcount:
                    return (
                        OutboxItem(
                            id=int(cursor.lastrowid or 0),
                            account_id=account_id,
                            chat_id=chat_id,
                            text=text,
                            idempotency_key=key,
                            created_at=now,
                        ),
                        True,
                    )
                existing = await self.get_outbox_by_key(key)
                if existing is None:
                    continue
                if (now - existing.created_at).total_seconds() <= dedup_window_seconds:
                    return existing, False
                key = f"{idempotency_key}:{attempt + 1}:{int(now.timestamp())}"
        raise RuntimeError("не удалось поставить сообщение в очередь: коллизия ключей")

    async def get_outbox_by_key(self, idempotency_key: str) -> OutboxItem | None:
        async with self.db.execute(
            "SELECT * FROM outbox WHERE idempotency_key = ?", (idempotency_key,)
        ) as cursor:
            row = await cursor.fetchone()
        return outbox_row(row) if row else None

    async def get_outbox(self, item_id: int) -> OutboxItem | None:
        async with self.db.execute("SELECT * FROM outbox WHERE id = ?", (item_id,)) as cursor:
            row = await cursor.fetchone()
        return outbox_row(row) if row else None

    async def outbox_stats(self) -> dict[str, int]:
        """Сводка по состояниям очереди.

        Состояния без записей показываются нулями: иначе по выводу нельзя
        отличить «таких записей нет» от «это состояние не считают», и новое
        состояние молча пропало бы из статуса.
        """
        stats = {state.value: 0 for state in OutboxState}
        async with self.db.execute(
            "SELECT state, COUNT(*) AS total FROM outbox GROUP BY state"
        ) as cursor:
            rows = await cursor.fetchall()
        for row in rows:
            stats[row["state"]] = row["total"]
        return stats
