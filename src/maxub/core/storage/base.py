"""Подключение к БД, обновление схемы и права доступа.

Прикладная база физически отделена от сессионного хранилища транспортной
библиотеки: у них разные схемы, миграции и владельцы. Совпадение технологии
(обе на SQLite) — не повод класть их в один файл.

Само описание схемы лежит в :mod:`maxub.core.storage.migrations`: база
обновляется на месте, поэтому схема существует только как история шагов.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

import aiosqlite

from maxub.core.crypto import SecretBox
from maxub.core.storage.migrations import SCHEMA_VERSION, SchemaVersionError, apply_migrations

# Сколько ждать освобождения блокировки записи. Пять секунд перекрывают
# миграцию прикладной базы и одновременный старт нескольких процессов, но не
# превращают взаимную блокировку в вечное ожидание.
BUSY_TIMEOUT_MS = 5000

__all__ = [
    "BUSY_TIMEOUT_MS",
    "SCHEMA_VERSION",
    "Database",
    "DuplicateAccountError",
    "SchemaVersionError",
    "parse_dt",
]


def parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


class DuplicateAccountError(Exception):
    """Аккаунт с таким телефоном уже есть."""

    def __init__(self, phone: str) -> None:
        super().__init__(f"аккаунт {phone} уже добавлен")
        self.phone = phone


class Database:
    """Соединение с БД и её обслуживание."""

    def __init__(self, path: Path, secrets_box: SecretBox) -> None:
        self._path = path
        self._secrets = secrets_box
        self._db: aiosqlite.Connection | None = None
        self._write_lock = asyncio.Lock()
        self._writer: asyncio.Task[Any] | None = None

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("хранилище не открыто")
        return self._db

    @asynccontextmanager
    async def write(self) -> AsyncIterator[aiosqlite.Connection]:
        """Единственный способ что-либо изменить в базе.

        Соединение с БД одно на весь демон, а sqlite3 держит транзакцию
        открытой от первого изменяющего оператора до ``commit``. Значит любой
        ``commit`` фиксирует всё, что накопилось на соединении, — включая чужую
        наполовину сделанную работу. Пока операция состоит из одного оператора,
        это незаметно; как только между операторами появляется ``await``,
        соседняя корутина успевает зафиксировать половину чужой транзакции, и
        падение сразу после этого оставляет базу в состоянии, которое операция
        как раз и обязана была исключить (сообщение отправлено, события о нём
        нет).

        Поэтому изменения сериализуются замком: пишет одна корутина за раз,
        фиксация и откат принадлежат тому же блоку, что и операторы. Гарантия
        держится только целиком — один писатель мимо ``write`` возвращает
        исходную дыру, поэтому через этот менеджер проходят все изменяющие
        методы примесей без исключений.

        Вход повторный в пределах одной задачи: составные операции собираются
        из более мелких (``mark_sent_with_event`` пишет событие через
        ``insert_event``, ``enqueue`` перечитывает очередь по ключу), а обычный
        ``asyncio.Lock`` не реентерабелен и на вложенном захвате повис бы
        навсегда. Вложенный блок ничего не фиксирует и не откатывает: он часть
        внешней транзакции, и её судьбу решает внешний блок.

        Чтения замком не закрываются. На одном соединении ``SELECT`` не может
        увидеть незафиксированные данные другого соединения, а конкурировать за
        доступ к файлу читателю не с кем: писатель здесь ровно один. Взамен
        читатель может увидеть незавершённую транзакцию писателя — но это
        строго лучше прежнего поведения, где та же половина работы попадала к
        нему уже зафиксированной, то есть навсегда.
        """
        holder = asyncio.current_task()
        if holder is not None and self._writer is holder:
            yield self.db
            return
        async with self._write_lock:
            self._writer = holder
            try:
                yield self.db
            except BaseException:
                # BaseException, а не Exception: снятая посреди записи задача
                # тоже обязана оставить соединение без открытой транзакции —
                # иначе её обрывок заберёт себе следующий писатель.
                await self.db.rollback()
                raise
            else:
                await self.db.commit()
            finally:
                self._writer = None

    async def open(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self._path)
        self._db.row_factory = aiosqlite.Row
        # Права закрываются сразу после создания файла, а не только в конце:
        # между открытием и концом миграций база уже существует и доступна на
        # чтение всем, кому позволяет umask.
        self._restrict_permissions()
        await self._db.execute("PRAGMA journal_mode=WAL")
        # Внешние ключи включаются до миграций: внутри транзакции этот PRAGMA
        # молча игнорируется.
        await self._db.execute("PRAGMA foreign_keys=ON")
        # Без ожидания второй демон, стартовавший одновременно с первым, упал
        # бы на «database is locked» вместо того, чтобы дождаться миграции.
        await self._db.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS:d}")
        # Миграции идут мимо write(): они управляют транзакцией сами
        # (``BEGIN IMMEDIATE`` внутри write() упёрся бы в уже открытую
        # транзакцию), и на этом этапе конкурировать с ними не с кем — база
        # ещё никому не отдана, а замок берут только методы примесей.
        #
        # Неудачная миграция не должна оставлять открытое соединение: демон
        # завершится с ошибкой, а незакрытый WAL пережил бы процесс.
        try:
            await apply_migrations(self._db)
        except BaseException:
            self._restrict_permissions()
            await self.close()
            raise
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
