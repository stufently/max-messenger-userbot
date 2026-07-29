"""Прикладная БД.

Физически отделена от сессионного хранилища транспортной библиотеки: у них
разные схемы, миграции и владельцы. Совпадение технологии (обе на SQLite) —
не повод класть их в один файл.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import aiosqlite

from maxub.core.models import Account, AccountState, Event, OutboxItem, OutboxState, utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    phone       TEXT NOT NULL UNIQUE,
    label       TEXT,
    state       TEXT NOT NULL,
    last_error  TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    account_id  INTEGER PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE,
    payload     TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS outbox (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id        INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    chat_id           TEXT NOT NULL,
    text              TEXT NOT NULL,
    idempotency_key   TEXT NOT NULL UNIQUE,
    state             TEXT NOT NULL,
    attempts          INTEGER NOT NULL DEFAULT 0,
    remote_message_id TEXT,
    error             TEXT,
    created_at        TEXT NOT NULL,
    claimed_at        TEXT,
    sent_at           TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER,
    kind       TEXT NOT NULL,
    payload    TEXT NOT NULL,
    dedup_key  TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sync_cursor (
    account_id INTEGER PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE,
    cursor     TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


_ENQUEUE_KEY_ATTEMPTS = 5
_UNKNOWN_OUTCOME = (
    "исход отправки неизвестен: процесс остановлен между обращением к транспорту"
    " и записью результата, повтор вручную"
)


class DuplicateAccountError(Exception):
    """Аккаунт с таким телефоном уже есть."""

    def __init__(self, phone: str) -> None:
        super().__init__(f"аккаунт {phone} уже добавлен")
        self.phone = phone


class Storage:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._db: aiosqlite.Connection | None = None

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("хранилище не открыто")
        return self._db

    async def open(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self._path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA foreign_keys=ON")
        await self._db.executescript(SCHEMA)
        await self._db.commit()
        self._restrict_permissions()

    def _restrict_permissions(self) -> None:
        """Закрывает БД от чтения посторонними.

        В базе лежат сессии аккаунтов, а SQLite создаёт файлы по umask, то есть
        обычно 0644. Права выставляются и на WAL с SHM — данные попадают и туда.
        """
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(f"{self._path}{suffix}")
            if candidate.exists():
                candidate.chmod(0o600)

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    # --- аккаунты -----------------------------------------------------------

    async def add_account(self, phone: str, label: str | None) -> Account:
        """Добавляет аккаунт.

        Возвращает ``None``-безопасный результат: гонка двух одновременных
        запросов разрешается уникальным индексом, а не проверкой до вставки.
        """
        now = utcnow()
        try:
            cursor = await self.db.execute(
                "INSERT INTO accounts (phone, label, state, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (phone, label, AccountState.AUTH_REQUIRED.value, now.isoformat(), now.isoformat()),
            )
        except aiosqlite.IntegrityError as exc:
            raise DuplicateAccountError(phone) from exc
        await self.db.commit()
        account_id = int(cursor.lastrowid or 0)
        return Account(
            id=account_id,
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
        await self.db.execute(
            "UPDATE accounts SET state = ?, last_error = ?, updated_at = ? WHERE id = ?",
            (state.value, error, utcnow().isoformat(), account_id),
        )
        await self.db.commit()

    @staticmethod
    def _account(row: aiosqlite.Row) -> Account:
        return Account(
            id=row["id"],
            phone=row["phone"],
            label=row["label"],
            state=AccountState(row["state"]),
            last_error=row["last_error"],
            created_at=_dt(row["created_at"]),
            updated_at=_dt(row["updated_at"]),
        )

    # --- сессии -------------------------------------------------------------

    async def save_session(self, account_id: int, payload: dict[str, Any]) -> None:
        await self.db.execute(
            "INSERT INTO sessions (account_id, payload, updated_at) VALUES (?, ?, ?)"
            " ON CONFLICT(account_id) DO UPDATE SET payload = excluded.payload,"
            " updated_at = excluded.updated_at",
            (account_id, json.dumps(payload), utcnow().isoformat()),
        )
        await self.db.commit()

    async def load_session(self, account_id: int) -> dict[str, Any] | None:
        async with self.db.execute(
            "SELECT payload FROM sessions WHERE account_id = ?", (account_id,)
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        loaded: dict[str, Any] = json.loads(row["payload"])
        return loaded

    # --- очередь отправки ---------------------------------------------------

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
        """
        now = utcnow()
        key = idempotency_key
        for attempt in range(_ENQUEUE_KEY_ATTEMPTS):
            cursor = await self.db.execute(
                "INSERT INTO outbox"
                " (account_id, chat_id, text, idempotency_key, state, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(idempotency_key) DO NOTHING",
                (account_id, chat_id, text, key, OutboxState.QUEUED.value, now.isoformat()),
            )
            await self.db.commit()
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
        return self._outbox(row) if row else None

    async def get_outbox(self, item_id: int) -> OutboxItem | None:
        async with self.db.execute("SELECT * FROM outbox WHERE id = ?", (item_id,)) as cursor:
            row = await cursor.fetchone()
        return self._outbox(row) if row else None

    async def claim_queued(self, limit: int = 10) -> list[OutboxItem]:
        """Атомарно забирает пачку сообщений в работу.

        Выборка и перевод в ``sending`` выполняются одним оператором: иначе два
        воркера успели бы прочитать одни и те же строки и отправить сообщение
        дважды.
        """
        now = utcnow().isoformat()
        async with self.db.execute(
            "UPDATE outbox SET state = ?, attempts = attempts + 1, claimed_at = ?"
            " WHERE id IN (SELECT id FROM outbox WHERE state = ? ORDER BY id LIMIT ?)"
            " RETURNING *",
            (OutboxState.SENDING.value, now, OutboxState.QUEUED.value, limit),
        ) as cursor:
            rows = await cursor.fetchall()
        await self.db.commit()
        return [self._outbox(row) for row in rows]

    async def recover_stale_sending(self) -> list[OutboxItem]:
        """Разбирает записи, застрявшие в ``sending`` после падения процесса.

        Такие сообщения могли уйти получателю: между успешным вызовом
        транспорта и записью результата процесс мог умереть. Переотправлять их
        вслепую нельзя — это дало бы дубль у получателя, поэтому они помечаются
        как неуспешные с явной причиной и ждут решения человека.
        """
        async with self.db.execute(
            "UPDATE outbox SET state = ?, error = ? WHERE state = ? RETURNING *",
            (OutboxState.FAILED.value, _UNKNOWN_OUTCOME, OutboxState.SENDING.value),
        ) as cursor:
            rows = await cursor.fetchall()
        await self.db.commit()
        return [self._outbox(row) for row in rows]

    async def mark_sent(self, item_id: int, remote_message_id: str) -> None:
        await self.db.execute(
            "UPDATE outbox SET state = ?, remote_message_id = ?, sent_at = ?, error = NULL"
            " WHERE id = ?",
            (OutboxState.SENT.value, remote_message_id, utcnow().isoformat(), item_id),
        )
        await self.db.commit()

    async def mark_failed(self, item_id: int, error: str) -> None:
        await self.db.execute(
            "UPDATE outbox SET state = ?, error = ? WHERE id = ?",
            (OutboxState.FAILED.value, error, item_id),
        )
        await self.db.commit()

    async def requeue(self, item_id: int) -> None:
        await self.db.execute(
            "UPDATE outbox SET state = ? WHERE id = ?", (OutboxState.QUEUED.value, item_id)
        )
        await self.db.commit()

    async def outbox_stats(self) -> dict[str, int]:
        async with self.db.execute(
            "SELECT state, COUNT(*) AS total FROM outbox GROUP BY state"
        ) as cursor:
            rows = await cursor.fetchall()
        return {row["state"]: row["total"] for row in rows}

    @staticmethod
    def _outbox(row: aiosqlite.Row) -> OutboxItem:
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
            created_at=_dt(row["created_at"]),
            claimed_at=_dt(row["claimed_at"]) if row["claimed_at"] else None,
            sent_at=_dt(row["sent_at"]) if row["sent_at"] else None,
        )

    # --- события ------------------------------------------------------------

    async def record_event(self, event: Event) -> bool:
        """Пишет событие. ``False`` — дубликат, который уже видели."""
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
            return False
        await self.db.commit()
        return True

    async def list_events(self, limit: int = 50, after_id: int = 0) -> list[tuple[int, Event]]:
        async with self.db.execute(
            "SELECT * FROM events WHERE id > ? ORDER BY id LIMIT ?", (after_id, limit)
        ) as cursor:
            rows = await cursor.fetchall()
        return [
            (
                row["id"],
                Event(
                    account_id=row["account_id"],
                    kind=row["kind"],
                    payload=json.loads(row["payload"]),
                    dedup_key=row["dedup_key"],
                    created_at=_dt(row["created_at"]),
                ),
            )
            for row in rows
        ]

    # --- курсор синхронизации ----------------------------------------------

    async def save_cursor(self, account_id: int, cursor_value: str) -> None:
        await self.db.execute(
            "INSERT INTO sync_cursor (account_id, cursor, updated_at) VALUES (?, ?, ?)"
            " ON CONFLICT(account_id) DO UPDATE SET cursor = excluded.cursor,"
            " updated_at = excluded.updated_at",
            (account_id, cursor_value, utcnow().isoformat()),
        )
        await self.db.commit()

    async def load_cursor(self, account_id: int) -> str | None:
        async with self.db.execute(
            "SELECT cursor FROM sync_cursor WHERE account_id = ?", (account_id,)
        ) as cursor:
            row = await cursor.fetchone()
        return row["cursor"] if row else None
