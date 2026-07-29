"""Журнал событий."""

from __future__ import annotations

import json
from datetime import datetime

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

    async def first_event_id(self) -> int | None:
        """Идентификатор самого старого сохранившегося события."""
        async with self.db.execute("SELECT MIN(id) AS oldest FROM events") as cursor:
            row = await cursor.fetchone()
        if row is None or row["oldest"] is None:
            return None
        return int(row["oldest"])

    async def last_event_id(self) -> int:
        """Идентификатор последнего события; 0 — журнал пуст."""
        async with self.db.execute("SELECT MAX(id) AS newest FROM events") as cursor:
            row = await cursor.fetchone()
        if row is None or row["newest"] is None:
            return 0
        return int(row["newest"])

    async def prune_events(self, older_than: datetime, keep_from_id: int | None = None) -> int:
        """Удаляет события старше указанного момента, возвращает их число.

        Сравнение идёт по строке, а не по разобранной дате: `created_at` всегда
        пишется как `utcnow().isoformat()`, то есть в UTC и с одинаковым видом
        смещения, а такие строки сравниваются лексикографически в том же
        порядке, что и сами моменты времени. Разбирать дату в SQL значило бы
        отказаться от индексируемого сравнения ради того же результата.

        Курсор `after_id` от подрезки не страдает: идентификаторы растут и не
        переиспользуются, поэтому клиент, стоящий на старом `id`, просто
        получит следующие сохранившиеся события, а не начнёт сначала. Это верно
        для того, кто читает журнал сам и волен пропустить удалённое. Для
        обработчика событий — нет: он обязан увидеть каждое своё событие, и
        уборка, обогнавшая его курсор, молча съела бы работу. Поэтому
        ``keep_from_id`` задаёт границу, за которую уборка не заходит, — самую
        отстающую позицию среди подключённых обработчиков.
        """
        query = "DELETE FROM events WHERE created_at < ?"
        params: list[object] = [older_than.isoformat()]
        if keep_from_id is not None:
            query += " AND id <= ?"
            params.append(keep_from_id)
        async with self.write() as db:
            cursor = await db.execute(query, tuple(params))
        return int(cursor.rowcount)

    @staticmethod
    def _event(row: aiosqlite.Row) -> Event:
        return Event(
            account_id=row["account_id"],
            kind=row["kind"],
            payload=json.loads(row["payload"]),
            dedup_key=row["dedup_key"],
            created_at=parse_dt(row["created_at"]),
        )
