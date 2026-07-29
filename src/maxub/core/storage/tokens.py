"""Выпущенные токены API.

Хранится отпечаток, а не сам токен: см. миграцию 4. Поиск идёт по отпечатку и
попадает в уникальный индекс, поэтому проверка токена — это одно чтение по
индексу, независимо от числа выданных токенов.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import aiosqlite

from maxub.core.models import ApiToken, utcnow
from maxub.core.permissions import Scope, format_scopes, parse_scopes
from maxub.core.storage.base import Database, parse_dt

#: Как часто отмечать, что токеном пользовались. Отметка нужна человеку —
#: понять, какой из выданных токенов ещё живой, а какой пора отозвать. Точность
#: до минуты для этого избыточна, а запись на каждый запрос превратила бы любое
#: чтение API в запись в базу, то есть в очередь за общим замком писателя.
TOUCH_INTERVAL = timedelta(minutes=1)


class TokensMixin(Database):
    async def create_token(
        self,
        label: str,
        token_hash: str,
        scopes: frozenset[Scope],
        expires_at: datetime | None = None,
    ) -> ApiToken:
        created_at = utcnow()
        async with self.write() as db:
            cursor = await db.execute(
                "INSERT INTO api_tokens (label, token_hash, scopes, created_at, expires_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    label,
                    token_hash,
                    format_scopes(scopes),
                    created_at.isoformat(),
                    expires_at.isoformat() if expires_at else None,
                ),
            )
        return ApiToken(
            id=int(cursor.lastrowid or 0),
            label=label,
            scopes=scopes,
            created_at=created_at,
            expires_at=expires_at,
        )

    async def find_token_by_hash(self, token_hash: str) -> ApiToken | None:
        async with self.db.execute(
            "SELECT * FROM api_tokens WHERE token_hash = ?", (token_hash,)
        ) as cursor:
            row = await cursor.fetchone()
        return self._token(row) if row is not None else None

    async def get_token(self, token_id: int) -> ApiToken | None:
        async with self.db.execute("SELECT * FROM api_tokens WHERE id = ?", (token_id,)) as cursor:
            row = await cursor.fetchone()
        return self._token(row) if row is not None else None

    async def list_tokens(self, include_revoked: bool = False) -> list[ApiToken]:
        """Выданные токены. Отозванные по умолчанию не показываются.

        Строка отозванного токена остаётся в базе: отпечаток занят, и тот же
        секрет невозможно выпустить повторно, а человек по списку с флагом
        видит, что токен был и когда его закрыли.
        """
        query = "SELECT * FROM api_tokens"
        if not include_revoked:
            query += " WHERE revoked_at IS NULL"
        async with self.db.execute(query + " ORDER BY id") as cursor:
            rows = await cursor.fetchall()
        return [self._token(row) for row in rows]

    async def revoke_token(self, token_id: int) -> bool:
        """Отзывает токен; ``False`` — такого нет или он уже отозван."""
        async with self.write() as db:
            cursor = await db.execute(
                "UPDATE api_tokens SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
                (utcnow().isoformat(), token_id),
            )
        return cursor.rowcount > 0

    async def touch_token(self, token_id: int, now: datetime | None = None) -> None:
        """Отмечает использование токена не чаще, чем раз в ``TOUCH_INTERVAL``.

        Условие стоит в самом ``UPDATE``, а не в коде до него: два запроса,
        пришедшие одновременно, иначе оба увидели бы старую отметку и оба пошли
        бы писать. Здесь второй просто не найдёт строку под условие.
        """
        moment = now or utcnow()
        cutoff = (moment - TOUCH_INTERVAL).isoformat()
        async with self.write() as db:
            await db.execute(
                "UPDATE api_tokens SET last_used_at = ?"
                " WHERE id = ? AND (last_used_at IS NULL OR last_used_at < ?)",
                (moment.isoformat(), token_id, cutoff),
            )

    @staticmethod
    def _token(row: aiosqlite.Row) -> ApiToken:
        return ApiToken(
            id=row["id"],
            label=row["label"],
            scopes=parse_scopes(str(row["scopes"]).split()),
            created_at=parse_dt(row["created_at"]),
            expires_at=parse_dt(row["expires_at"]) if row["expires_at"] else None,
            last_used_at=parse_dt(row["last_used_at"]) if row["last_used_at"] else None,
            revoked_at=parse_dt(row["revoked_at"]) if row["revoked_at"] else None,
        )
