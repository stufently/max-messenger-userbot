"""Обновление схемы на месте.

Проверяется главное свойство миграций: база, пережившая обновление демона,
доходит до актуальной версии вместе с данными, а несовместимая версия
останавливает запуск, а не отказывает потом в случайном месте.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import aiosqlite
import pytest

from maxub.core.crypto import SecretBox, generate_key
from maxub.core.storage import migrations as migrations_module
from maxub.core.storage.base import Database
from maxub.core.storage.migrations import (
    MIGRATIONS,
    SCHEMA_VERSION,
    Migration,
    SchemaVersionError,
    apply_migrations,
    read_schema_version,
)

# Схема версии 0.1.0 — до появления счётчика версий, отложенных повторов и
# штрафов лимитера. Записана буквально, а не собрана из кода: смысл теста в
# том, чтобы сверяться с прошлым, которое уже нельзя изменить.
LEGACY_SCHEMA = """
CREATE TABLE accounts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    phone       TEXT NOT NULL UNIQUE,
    label       TEXT,
    state       TEXT NOT NULL,
    last_error  TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE sessions (
    account_id  INTEGER PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE,
    payload     TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE outbox (
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

CREATE TABLE events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER,
    kind       TEXT NOT NULL,
    payload    TEXT NOT NULL,
    dedup_key  TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE TABLE sync_cursor (
    account_id INTEGER PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE,
    cursor     TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def build_database(path: Path) -> Database:
    return Database(path, SecretBox(generate_key()))


def make_legacy_db(path: Path) -> None:
    """Создаёт базу предыдущей версии с данными внутри."""
    with sqlite3.connect(path) as raw:
        raw.executescript(LEGACY_SCHEMA)
        raw.execute(
            "INSERT INTO accounts (id, phone, label, state, created_at, updated_at)"
            " VALUES (1, '+79990000000', 'старый', 'online', '2026-01-01T00:00:00',"
            " '2026-01-01T00:00:00')",
        )
        raw.execute(
            "INSERT INTO outbox (account_id, chat_id, text, idempotency_key, state, created_at)"
            " VALUES (1, 'chat', 'привет', 'key-1', 'queued', '2026-01-01T00:00:00')",
        )
        raw.commit()
    raw.close()


def user_version(path: Path) -> int:
    raw = sqlite3.connect(path)
    try:
        return int(raw.execute("PRAGMA user_version").fetchone()[0])
    finally:
        raw.close()


def set_user_version(path: Path, version: int) -> None:
    raw = sqlite3.connect(path)
    try:
        raw.execute(f"PRAGMA user_version = {version:d}")
    finally:
        raw.close()


async def columns(db: Database, table: str) -> set[str]:
    async with db.db.execute(f"PRAGMA table_info({table})") as cursor:
        rows = await cursor.fetchall()
    return {row["name"] for row in rows}


async def test_fresh_database_gets_current_version(tmp_path: Path) -> None:
    db = build_database(tmp_path / "fresh.db")
    await db.open()
    try:
        assert await columns(db, "outbox") >= {"next_attempt_at", "claimed_at"}
        async with db.db.execute("SELECT COUNT(*) AS total FROM rate_penalty") as cursor:
            assert (await cursor.fetchone())["total"] == 0
    finally:
        await db.close()
    assert user_version(tmp_path / "fresh.db") == SCHEMA_VERSION


async def test_legacy_database_is_upgraded_without_data_loss(tmp_path: Path) -> None:
    path = tmp_path / "legacy.db"
    make_legacy_db(path)
    assert user_version(path) == 0

    db = build_database(path)
    await db.open()
    try:
        assert {"next_attempt_at", "discard_reason", "discarded_at"} <= await columns(db, "outbox")
        # Новый столбец у старых строк пуст — планировщик считает такие
        # сообщения готовыми к отправке, это и есть ожидаемое поведение.
        async with db.db.execute("SELECT * FROM outbox") as cursor:
            rows = await cursor.fetchall()
        assert [(row["idempotency_key"], row["next_attempt_at"]) for row in rows] == [
            ("key-1", None)
        ]
        async with db.db.execute("SELECT phone FROM accounts") as cursor:
            assert [row["phone"] for row in await cursor.fetchall()] == ["+79990000000"]
        # Таблица штрафов появилась и пригодна к записи.
        await db.db.execute(
            "INSERT INTO rate_penalty (account_id, action, until)"
            " VALUES (1, 'send', '2026-01-01T00:00:00')"
        )
        await db.db.commit()
    finally:
        await db.close()
    assert user_version(path) == SCHEMA_VERSION


async def make_previous_version_db(path: Path) -> None:
    """База версии, предшествующей текущей, с отказавшей записью внутри.

    Собирается шагами самих миграций, а не отдельным DDL: смысл теста — путь
    «предыдущий выпуск демона обновился до этого», и стартовая точка обязана
    совпадать с тем, что тот выпуск действительно создавал.
    """
    raw = await aiosqlite.connect(path)
    try:
        for migration in MIGRATIONS[:-1]:
            for statement in migration.statements:
                await raw.execute(statement)
        await raw.execute(f"PRAGMA user_version = {MIGRATIONS[-2].version:d}")
        await raw.execute(
            "INSERT INTO accounts (id, phone, state, created_at, updated_at)"
            " VALUES (1, '+79990000009', 'ready', '2026-01-01T00:00:00', '2026-01-01T00:00:00')"
        )
        await raw.execute(
            "INSERT INTO outbox (id, account_id, chat_id, text, idempotency_key, state,"
            " attempts, error, created_at)"
            " VALUES (7, 1, 'chat', 'до обновления', 'key-old', 'failed', 3, 'канал занят',"
            " '2026-01-01T00:00:00')"
        )
        await raw.commit()
    finally:
        await raw.close()


async def test_previous_version_learns_to_store_refusals(tmp_path: Path) -> None:
    """База прошлого выпуска доводится до актуальной, и отказ в ней уже пишется.

    Проверяется не только появление столбцов: важно, что старая отказавшая
    запись пережила обновление вместе с ошибкой, ради которой её и оставили
    человеку.
    """
    path = tmp_path / "previous.db"
    await make_previous_version_db(path)
    assert user_version(path) == SCHEMA_VERSION - 1

    db = build_database(path)
    await db.open()
    try:
        assert {"discard_reason", "discarded_at"} <= await columns(db, "outbox")
        async with db.db.execute("SELECT * FROM outbox WHERE id = 7") as cursor:
            row = await cursor.fetchone()
        assert row is not None
        # Данные на месте, новые столбцы пусты — записи, разобранные до
        # обновления, не притворяются отказанными.
        assert (row["text"], row["state"], row["attempts"]) == ("до обновления", "failed", 3)
        assert (row["error"], row["discard_reason"], row["discarded_at"]) == (
            "канал занят",
            None,
            None,
        )
        await db.db.execute(
            "UPDATE outbox SET state = 'discarded', discard_reason = 'устарело',"
            " discarded_at = '2026-02-01T00:00:00' WHERE id = 7"
        )
        await db.db.commit()
    finally:
        await db.close()
    assert user_version(path) == SCHEMA_VERSION

    # Повторное открытие ничего не переделывает: отказ остаётся отказом.
    again = build_database(path)
    await again.open()
    try:
        async with again.db.execute("SELECT * FROM outbox WHERE id = 7") as cursor:
            row = await cursor.fetchone()
        assert row is not None
        assert (row["state"], row["discard_reason"]) == ("discarded", "устарело")
    finally:
        await again.close()
    assert user_version(path) == SCHEMA_VERSION


async def test_reopening_current_database_changes_nothing(tmp_path: Path) -> None:
    path = tmp_path / "repeat.db"
    first = build_database(path)
    await first.open()
    await first.db.execute(
        "INSERT INTO accounts (phone, state, created_at, updated_at)"
        " VALUES ('+79990000001', 'new', '2026-01-01T00:00:00', '2026-01-01T00:00:00')"
    )
    await first.db.commit()
    await first.close()

    second = build_database(path)
    await second.open()
    try:
        async with second.db.execute("SELECT COUNT(*) AS total FROM accounts") as cursor:
            assert (await cursor.fetchone())["total"] == 1
    finally:
        await second.close()
    assert user_version(path) == SCHEMA_VERSION


async def test_database_from_the_future_refuses_to_open(tmp_path: Path) -> None:
    path = tmp_path / "future.db"
    db = build_database(path)
    await db.open()
    await db.close()
    set_user_version(path, SCHEMA_VERSION + 1)

    later = build_database(path)
    with pytest.raises(SchemaVersionError) as excinfo:
        await later.open()
    assert str(SCHEMA_VERSION + 1) in str(excinfo.value)
    # Соединение не должно остаться открытым после отказа.
    with pytest.raises(RuntimeError):
        _ = later.db


async def test_second_process_does_not_apply_migrations_twice(tmp_path: Path) -> None:
    """Два демона, стартовавшие одновременно, видят одну и ту же нулевую версию.

    Проверка того, что второй не выполнит ``ALTER TABLE`` повторно: версия
    перечитывается уже под блокировкой записи.
    """
    path = tmp_path / "race.db"
    make_legacy_db(path)
    first = await aiosqlite.connect(path)
    second = await aiosqlite.connect(path)
    try:
        assert await read_schema_version(first) == 0
        assert await read_schema_version(second) == 0
        assert await apply_migrations(first) == SCHEMA_VERSION
        # Второй по-прежнему «помнит» нулевую версию, но повтора шагов не будет.
        assert await apply_migrations(second) == SCHEMA_VERSION
    finally:
        await first.close()
        await second.close()
    assert user_version(path) == SCHEMA_VERSION


async def test_failed_step_leaves_schema_and_version_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "broken.db"
    broken = Migration(
        version=SCHEMA_VERSION + 1,
        summary="намеренно нерабочий шаг",
        statements=("CREATE TABLE marker (x INTEGER)", "ЭТО НЕ SQL"),
    )
    monkeypatch.setattr(migrations_module, "MIGRATIONS", (*migrations_module.MIGRATIONS, broken))
    monkeypatch.setattr(migrations_module, "SCHEMA_VERSION", broken.version)

    db = build_database(path)
    with pytest.raises(sqlite3.OperationalError):
        await db.open()

    # Ни таблицы из упавшего шага, ни его версии в БД остаться не должно.
    assert user_version(path) == SCHEMA_VERSION
    raw = sqlite3.connect(path)
    try:
        found = raw.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {row[0] for row in found}
    finally:
        raw.close()
    assert "marker" not in tables
    assert {"accounts", "outbox", "rate_penalty"} <= tables


async def test_database_files_permissions_are_restricted(tmp_path: Path) -> None:
    path = tmp_path / "perms.db"
    db = build_database(path)
    # Миграции сами пишут в базу, поэтому WAL и SHM существуют уже после
    # открытия — секреты попадают и в них.
    await db.open()
    try:
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(f"{path}{suffix}")
            assert candidate.exists(), suffix
            assert candidate.stat().st_mode & 0o777 == 0o600, suffix
    finally:
        await db.close()
