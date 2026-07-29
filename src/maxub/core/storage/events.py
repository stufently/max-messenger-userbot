"""Журнал событий."""

from __future__ import annotations

import json

import aiosqlite

from maxub.core.models import Event
from maxub.core.storage.base import Database, parse_dt


class EventsMixin(Database):
    async def record_event(self, event: Event) -> bool:
        """Пишет событие. ``False`` — дубликат, который уже видели.

        Уникальность ``dedup_key`` — это и есть защита от повторов: после
        переподключения сервер вполне может выдать те же события заново.
        """
        async with self.write():
            return await self.insert_event(event)

    async def insert_event(self, event: Event) -> bool:
        """То же, но без фиксации: событие пишется в текущую транзакцию.

        Нужно там, где событие обязано попасть в журнал вместе с изменением,
        которое оно описывает. Отдельная фиксация оставила бы окно, в котором
        сообщение уже помечено отправленным, а подписчики об этом никогда не
        узнают.

        Замок не берётся здесь намеренно: метод — часть чужой транзакции и
        вызывается только изнутри блока
        [write][maxub.core.storage.base.Database.write]. Собственный захват
        сделал бы вложенный вызов неотличимым от самостоятельной записи и
        зафиксировал бы половину внешней операции.
        """
        try:
            await self.db.execute(
                "INSERT INTO events (account_id, kind, payload, dedup_key, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    event.account_id,
                    event.kind,
                    json.dumps(event.payload, default=str),
                    event.dedup_key,
                    event.created_at.isoformat(),
                ),
            )
        except aiosqlite.IntegrityError:
            # Нарушение уникальности откатывает только сам оператор, поэтому
            # остальная транзакция остаётся пригодной для фиксации.
            return False
        return True

    async def list_events(self, limit: int = 50, after_id: int = 0) -> list[tuple[int, Event]]:
        async with self.db.execute(
            "SELECT * FROM events WHERE id > ? ORDER BY id LIMIT ?", (after_id, limit)
        ) as cursor:
            rows = await cursor.fetchall()
        return [(row["id"], self._event(row)) for row in rows]

    @staticmethod
    def _event(row: aiosqlite.Row) -> Event:
        return Event(
            account_id=row["account_id"],
            kind=row["kind"],
            payload=json.loads(row["payload"]),
            dedup_key=row["dedup_key"],
            created_at=parse_dt(row["created_at"]),
        )
