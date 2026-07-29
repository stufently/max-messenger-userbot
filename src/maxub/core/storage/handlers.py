"""Позиции обработчиков событий в журнале.

Журнал событий — источник правды и для обработчиков: подписка в памяти живёт
только пока жив процесс, а курсор в базе переживает перезапуск и падение. Из
этого следует гарантия «хотя бы один раз»: событие, обработанное перед самой
смертью процесса, придёт снова, потому что курсор сдвинуться не успел. Защита
от повторного действия — на стороне идемпотентности отправки, а не здесь.
"""

from __future__ import annotations

from collections.abc import Sequence

from maxub.core.models import Event, utcnow
from maxub.core.storage.events import EventsMixin


class HandlerCursorsMixin(EventsMixin):
    async def init_handler_cursor(self, name: str, start_after_id: int) -> int:
        """Заводит курсор, если его ещё нет, и отдаёт действующее значение.

        Новый обработчик начинает с конца журнала, а не с его начала: иначе
        первое же включение обработчика вывалило бы на него всю накопленную
        историю и, чего доброго, разослало бы ответы на трёхмесячной давности
        сообщения.
        """
        async with self.write() as db:
            await db.execute(
                "INSERT OR IGNORE INTO handler_cursors (name, after_id, attempts, updated_at)"
                " VALUES (?, ?, 0, ?)",
                (name, start_after_id, utcnow().isoformat()),
            )
        return await self.load_handler_cursor(name) or 0

    async def load_handler_cursor(self, name: str) -> int | None:
        async with self.db.execute(
            "SELECT after_id FROM handler_cursors WHERE name = ?", (name,)
        ) as cursor:
            row = await cursor.fetchone()
        return int(row["after_id"]) if row is not None else None

    async def load_handler_attempts(self, name: str) -> int:
        """Сколько раз подряд не далось событие, на котором стоит курсор."""
        async with self.db.execute(
            "SELECT attempts FROM handler_cursors WHERE name = ?", (name,)
        ) as cursor:
            row = await cursor.fetchone()
        return int(row["attempts"]) if row is not None else 0

    async def advance_handler_cursor(
        self, name: str, after_id: int, event: Event | None = None
    ) -> bool:
        """Двигает курсор вперёд, обнуляя счётчик попыток.

        Сопутствующее событие пишется в той же транзакции: «пропустили событие,
        но записи об этом нет» и «запись есть, а курсор на месте» — оба
        состояния одинаково вредны, а раздельная фиксация допускает и то и
        другое.

        Продвижение условное: строка меняется, только если курсор ещё не ушёл
        дальше. Два демона на одной базе — не штатный режим, но случай
        возможный, и отставший экземпляр не должен ни отматывать курсор назад,
        ни обнулять счётчик попыток по чужому событию, ни дописывать в журнал
        отказ по событию, которое сосед давно разобрал. Параллельную доставку
        это не отменяет: от неё защищает идемпотентность отправки.

        Возвращает, попало ли событие в журнал: устаревшее продвижение и повтор
        по ключу дедупликации — это ``False``, и раздавать такое подписчикам
        незачем.
        """
        written = False
        async with self.write() as db:
            moved = await db.execute(
                "UPDATE handler_cursors SET after_id = ?, attempts = 0, updated_at = ?"
                " WHERE name = ? AND after_id < ?",
                (after_id, utcnow().isoformat(), name, after_id),
            )
            if moved.rowcount == 0:
                return False
            if event is not None:
                written = await self.insert_event(event)
        return written

    async def bump_handler_attempts(self, name: str, after_id: int) -> int:
        """Считает неудачный подход к событию и отдаёт номер попытки.

        ``after_id`` — позиция, с которой это событие было взято. Счётчик
        принадлежит конкретному событию, а не обработчику вообще: если курсор
        успел уйти вперёд (сосед по базе разобрал то же событие), считать
        попытку не по чему, и метод отдаёт ноль — подход просто не засчитан.
        """
        async with self.write() as db:
            cursor = await db.execute(
                "UPDATE handler_cursors SET attempts = attempts + 1, updated_at = ?"
                " WHERE name = ? AND after_id = ?",
                (utcnow().isoformat(), name, after_id),
            )
            if cursor.rowcount == 0:
                return 0
        return await self.load_handler_attempts(name)

    async def handler_cursor_floor(self, names: Sequence[str]) -> int | None:
        """Самая отстающая позиция среди перечисленных обработчиков.

        Перечислять приходится явно: в таблице остаются курсоры обработчиков,
        которых в этой сборке уже нет, и учитывать их значило бы запретить
        уборку журнала навсегда из-за давно снятого обработчика.
        """
        if not names:
            return None
        placeholders = ", ".join("?" for _ in names)
        async with self.db.execute(
            f"SELECT MIN(after_id) AS floor FROM handler_cursors WHERE name IN ({placeholders})",
            tuple(names),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None or row["floor"] is None:
            return None
        return int(row["floor"])
