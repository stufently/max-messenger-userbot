"""Аккаунты, их сессии и позиция синхронизации."""

from __future__ import annotations

from typing import Any

import aiosqlite

from maxub.core.crypto import SecretBox
from maxub.core.models import Account, AccountState, Event, utcnow
from maxub.core.storage.base import DuplicateAccountError, parse_dt
from maxub.core.storage.events import EventsMixin


class AccountsMixin(EventsMixin):
    """Аккаунты и их состояния.

    Наследуется от журнала событий не ради удобства: смена состояния и запись
    события о ней обязаны попадать в одну транзакцию, а значит должны уметь
    писать в обе таблицы, не фиксируя изменения по отдельности.
    """

    async def add_account(self, phone: str, label: str | None) -> Account:
        """Добавляет аккаунт.

        Гонка двух одновременных запросов разрешается уникальным индексом, а не
        проверкой, выполненной до вставки.
        """
        now = utcnow()
        async with self.write() as db:
            try:
                cursor = await db.execute(
                    "INSERT INTO accounts (phone, label, state, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (
                        phone,
                        label,
                        AccountState.AUTH_REQUIRED.value,
                        now.isoformat(),
                        now.isoformat(),
                    ),
                )
            except aiosqlite.IntegrityError as exc:
                raise DuplicateAccountError(phone) from exc
        return Account(
            id=int(cursor.lastrowid or 0),
            phone=phone,
            label=label,
            state=AccountState.AUTH_REQUIRED,
            created_at=now,
            updated_at=now,
        )

    async def list_accounts(self) -> list[Account]:
        async with self.db.execute("SELECT * FROM accounts ORDER BY id") as cursor:
            rows = await cursor.fetchall()
        return [self._account(row) for row in rows]

    async def get_account(self, account_id: int) -> Account | None:
        async with self.db.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)) as cursor:
            row = await cursor.fetchone()
        return self._account(row) if row else None

    async def get_account_by_phone(self, phone: str) -> Account | None:
        async with self.db.execute("SELECT * FROM accounts WHERE phone = ?", (phone,)) as cursor:
            row = await cursor.fetchone()
        return self._account(row) if row else None

    async def set_account_state(
        self, account_id: int, state: AccountState, error: str | None = None
    ) -> None:
        async with self.write() as db:
            await self._write_state(db, account_id, state, error)

    async def set_account_state_with_event(
        self, account_id: int, state: AccountState, error: str | None, event: Event
    ) -> bool:
        """Меняет состояние и пишет событие об этом одной транзакцией.

        Двумя отдельными записями это было бы «состояние сменилось, а никто не
        узнал»: между ними процесс может умереть, задачу — отменить, а вторую
        запись — не пропустить журнал. Тот же приём, что и при закрытии
        отправки в
        [mark_sent_with_event][maxub.core.storage.delivery.DeliveryMixin.mark_sent_with_event],
        и по той же причине.

        Событие пишется только тогда, когда состояние действительно сменилось.
        Повторная запись того же состояния — обычное дело: запрос кода ставит
        аккаунту `auth_required`, а он в нём чаще всего и находится. Событие об
        этом означало бы переход, которого не было, и заполняло бы журнал шумом
        ровно там, где от него ждут сигнала.

        Ответ говорит, попало ли событие в журнал: раздавать подписчикам то, что
        не записано, нельзя — они увидели бы то, чего в истории нет.
        """
        async with self.write() as db:
            async with db.execute(
                "SELECT state FROM accounts WHERE id = ?", (account_id,)
            ) as cursor:
                row = await cursor.fetchone()
            changed = row is not None and AccountState(row["state"]) is not state
            await self._write_state(db, account_id, state, error)
            return await self.insert_event(event) if changed else False

    @staticmethod
    async def _write_state(
        db: aiosqlite.Connection, account_id: int, state: AccountState, error: str | None
    ) -> None:
        await db.execute(
            "UPDATE accounts SET state = ?, last_error = ?, updated_at = ? WHERE id = ?",
            (state.value, error, utcnow().isoformat(), account_id),
        )

    @staticmethod
    def _account(row: aiosqlite.Row) -> Account:
        return Account(
            id=row["id"],
            phone=row["phone"],
            label=row["label"],
            state=AccountState(row["state"]),
            last_error=row["last_error"],
            created_at=parse_dt(row["created_at"]),
            updated_at=parse_dt(row["updated_at"]),
        )

    # --- сессии -------------------------------------------------------------

    async def save_session(self, account_id: int, payload: dict[str, Any]) -> None:
        """Сохраняет сессию в зашифрованном виде.

        Ключ живёт вне базы, поэтому копия файла БД без ключа бесполезна.
        """
        async with self.write() as db:
            await db.execute(
                "INSERT INTO sessions (account_id, payload, updated_at) VALUES (?, ?, ?)"
                " ON CONFLICT(account_id) DO UPDATE SET payload = excluded.payload,"
                " updated_at = excluded.updated_at",
                (account_id, self._secrets.seal(payload), utcnow().isoformat()),
            )

    async def load_session(self, account_id: int) -> dict[str, Any] | None:
        async with self.db.execute(
            "SELECT payload FROM sessions WHERE account_id = ?", (account_id,)
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        return self._secrets.open(row["payload"])

    async def rotate_session_key(self, new_secrets: SecretBox) -> int:
        """Перешифровывает все сессии новым ключом.

        Записи обновляются одной транзакцией: прерывание посередине оставило бы
        часть сессий нечитаемыми.
        """
        async with self.write() as db:
            # Чтение внутри той же транзакции, что и запись: иначе между
            # выборкой и перешифровкой соседняя корутина сохранила бы сессию
            # старым ключом, и эта запись осталась бы нечитаемой.
            async with db.execute("SELECT account_id, payload FROM sessions") as cursor:
                rows = await cursor.fetchall()
            resealed = [
                (new_secrets.seal(self._secrets.open(row["payload"])), row["account_id"])
                for row in rows
            ]
            await db.executemany("UPDATE sessions SET payload = ? WHERE account_id = ?", resealed)
        # Ключ в памяти меняется только после фиксации: откат оставил бы в базе
        # сессии под старым ключом, а процесс читал бы их новым.
        self._secrets = new_secrets
        return len(resealed)

    # --- позиция синхронизации ----------------------------------------------

    async def save_cursor(self, account_id: int, cursor_value: str) -> None:
        async with self.write() as db:
            await db.execute(
                "INSERT INTO sync_cursor (account_id, cursor, updated_at) VALUES (?, ?, ?)"
                " ON CONFLICT(account_id) DO UPDATE SET cursor = excluded.cursor,"
                " updated_at = excluded.updated_at",
                (account_id, cursor_value, utcnow().isoformat()),
            )

    async def load_cursor(self, account_id: int) -> str | None:
        async with self.db.execute(
            "SELECT cursor FROM sync_cursor WHERE account_id = ?", (account_id,)
        ) as cursor:
            row = await cursor.fetchone()
        return str(row["cursor"]) if row else None
