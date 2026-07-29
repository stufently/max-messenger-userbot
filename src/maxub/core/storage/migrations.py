"""Версионированные миграции схемы прикладной БД.

Единственный источник правды о схеме — список ``MIGRATIONS``. Чистая база и
база предыдущей версии проходят один и тот же путь: первая применяет все шаги
подряд, вторая — только недостающие. Отдельной константы с «актуальным DDL»
намеренно нет: два описания схемы рано или поздно расходятся, и расхождение
обнаруживается уже у пользователя, а не в тестах. Цена решения — чистая база
создаётся не одним оператором, а историей шагов; на объёмах прикладной БД это
не имеет значения.

Номер версии живёт в ``PRAGMA user_version`` — счётчике внутри заголовка файла
БД. Он не требует служебной таблицы (иначе нулевая версия сама нуждалась бы в
миграции), переживает копирование файла и меняется в той же транзакции, что и
DDL шага.

Первый шаг воспроизводит схему версии 0.1.0 через ``IF NOT EXISTS``: базы,
созданные до появления счётчика версий, отдают ``user_version = 0``, и для них
этот шаг обязан быть безопасным повтором.
"""

from __future__ import annotations

from dataclasses import dataclass

import aiosqlite


@dataclass(frozen=True)
class Migration:
    """Один шаг обновления схемы: поднимает версию ровно на единицу."""

    version: int
    summary: str
    statements: tuple[str, ...]


MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        version=1,
        summary="базовая схема: аккаунты, сессии, очередь, события, курсоры",
        statements=(
            """
            CREATE TABLE IF NOT EXISTS accounts (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                phone       TEXT NOT NULL UNIQUE,
                label       TEXT,
                state       TEXT NOT NULL,
                last_error  TEXT,
                created_at  TEXT NOT NULL,
                updated_at  TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS sessions (
                account_id  INTEGER PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE,
                payload     TEXT NOT NULL,
                updated_at  TEXT NOT NULL
            )
            """,
            """
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
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS events (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER,
                kind       TEXT NOT NULL,
                payload    TEXT NOT NULL,
                dedup_key  TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS sync_cursor (
                account_id INTEGER PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE,
                cursor     TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
        ),
    ),
    Migration(
        version=2,
        summary="отложенные повторы отправки и штрафы лимитера",
        statements=(
            # Момент следующей попытки хранится в строке, а не вычисляется на
            # лету: после перезапуска демон обязан помнить, до какого времени
            # сообщение трогать нельзя.
            "ALTER TABLE outbox ADD COLUMN next_attempt_at TEXT",
            # Штрафы лимитера переживают перезапуск: иначе после рестарта демон
            # разом ломится на сервер, который только что попросил подождать.
            """
            CREATE TABLE IF NOT EXISTS rate_penalty (
                account_id INTEGER NOT NULL,
                action     TEXT NOT NULL,
                until      TEXT NOT NULL,
                PRIMARY KEY (account_id, action)
            )
            """,
        ),
    ),
)

SCHEMA_VERSION = MIGRATIONS[-1].version


class SchemaVersionError(RuntimeError):
    """БД новее, чем понимает этот код.

    Отдельный тип, а не общий сбой: сценарий здесь ровно один — откат
    приложения на предыдущую версию поверх уже обновлённой базы. Продолжать
    работу нельзя, потому что старый код будет молча игнорировать новые
    столбцы и портить данные.
    """

    def __init__(self, found: int, supported: int) -> None:
        super().__init__(
            f"версия схемы БД {found} новее поддерживаемой ({supported}):"
            " обновите max-userbot или восстановите базу из резервной копии"
        )
        self.found = found
        self.supported = supported


async def read_schema_version(db: aiosqlite.Connection) -> int:
    async with db.execute("PRAGMA user_version") as cursor:
        row = await cursor.fetchone()
    return int(row[0]) if row is not None else 0


async def apply_migrations(db: aiosqlite.Connection) -> int:
    """Доводит схему до ``SCHEMA_VERSION`` и возвращает итоговую версию.

    Версия перечитывается в конце: пока шли шаги, базу мог обновить соседний
    процесс с более новым кодом, и запускаться поверх чужой схемы нельзя даже
    в этом редком случае.
    """
    current = await read_schema_version(db)
    if current > SCHEMA_VERSION:
        raise SchemaVersionError(current, SCHEMA_VERSION)
    for migration in MIGRATIONS:
        if migration.version > current:
            await _apply(db, migration)
    final = await read_schema_version(db)
    if final > SCHEMA_VERSION:
        raise SchemaVersionError(final, SCHEMA_VERSION)
    return final


async def _apply(db: aiosqlite.Connection, migration: Migration) -> None:
    """Применяет один шаг целиком или не применяет вовсе.

    Транзакция открывается явно: в режиме совместимости sqlite3 сам её не
    начинает перед DDL, и прерванное обновление оставило бы половину схемы с
    неподнятой версией. Номер версии меняется в той же транзакции, поэтому
    «схема обновлена, а версия нет» невозможно.

    ``BEGIN IMMEDIATE`` с повторной проверкой версии внутри транзакции
    защищает от одновременного старта двух процессов: второй берёт блокировку
    записи уже после первого, видит поднятую версию и выходит. Без этого
    ``ALTER TABLE`` из того же шага выполнился бы дважды.
    """
    await db.execute("BEGIN IMMEDIATE")
    try:
        if await read_schema_version(db) >= migration.version:
            await db.rollback()
            return
        for statement in migration.statements:
            await db.execute(statement)
        # PRAGMA не принимает подстановки, поэтому версия вписывается в текст;
        # это целое число из кода, не пользовательский ввод.
        await db.execute(f"PRAGMA user_version = {migration.version:d}")
    except BaseException:
        # BaseException, а не Exception: отмена задачи посреди миграции тоже
        # обязана оставить схему в исходном состоянии.
        await db.rollback()
        raise
    await db.commit()
