"""Штрафы лимитера, пережившие перезапуск.

Сервер отвечает «повторите через N секунд», и это знание нельзя держать только
в памяти процесса: после перезапуска демон иначе пойдёт долбить сервер сразу и
получит штраф повторно, уже более суровый.
"""

from __future__ import annotations

from datetime import datetime

from maxub.core.storage.base import Database, parse_dt


class PenaltiesMixin(Database):
    async def save_penalty(self, account_id: int, action: str, until: datetime) -> None:
        async with self.write() as db:
            await db.execute(
                "INSERT INTO rate_penalty (account_id, action, until) VALUES (?, ?, ?)"
                " ON CONFLICT(account_id, action) DO UPDATE SET until = excluded.until",
                (account_id, action, until.isoformat()),
            )

    async def load_penalties(self) -> list[tuple[int, str, datetime]]:
        async with self.db.execute("SELECT * FROM rate_penalty") as cursor:
            rows = await cursor.fetchall()
        return [(row["account_id"], row["action"], parse_dt(row["until"])) for row in rows]

    async def clear_penalty(self, account_id: int, action: str) -> None:
        async with self.write() as db:
            await db.execute(
                "DELETE FROM rate_penalty WHERE account_id = ? AND action = ?",
                (account_id, action),
            )
