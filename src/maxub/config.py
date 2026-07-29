"""Конфигурация демона и клиентов. Читается из переменных окружения MAXUB_*."""

from __future__ import annotations

import os
import secrets
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from maxub.core import keystore
from maxub.core.crypto import generate_key
from maxub.core.keystore_backends import open_keystore

#: Сколько раз пытаться создать файл секрета, уступая соседнему процессу.
#: Больше двух витков подряд означает, что дело не в гонке, а в неисправности.
SECRET_CREATE_ATTEMPTS = 3

TOKEN_FILE = "api_token"
KEY_FILE = "secret.key"
DB_FILE = "maxub.db"


def _write_secret_file(path: Path, content: str) -> None:
    """Создаёт файл сразу с правами 0600.

    Вариант «записать, потом chmod» оставляет окно, в котором файл доступен по
    umask, — для секретов это неприемлемо.

    ``O_NOFOLLOW`` защищает от подмены пути символической ссылкой, но в Windows
    такого флага нет вовсе: обращение к нему через ``getattr`` — не небрежность,
    а единственный способ не уронить импорт там, где защищать нечего (права в
    каталоге профиля определяет ACL).
    """
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | nofollow, 0o600)
    try:
        os.write(fd, content.encode("utf-8"))
    finally:
        os.close(fd)


def _create_or_read_secret(path: Path, factory: Callable[[], str]) -> str:
    """Создаёт секрет либо возвращает уже созданный кем-то другим.

    Два процесса, стартовавшие одновременно на пустом каталоге, оба видят, что
    файла нет. `O_EXCL` не даёт им затереть работу друг друга, но проигравший
    получал `FileExistsError` и падал ещё до запуска. Проигравшему нужен не
    отказ, а значение победителя — оно уже на диске.
    """
    for _ in range(SECRET_CREATE_ATTEMPTS):
        if path.exists():
            existing = path.read_text(encoding="utf-8").strip()
            if existing:
                return existing
        value = factory()
        try:
            _write_secret_file(path, value)
        except FileExistsError:
            # Файл создал соседний процесс между проверкой и записью — читаем
            # его на следующем витке. Пустой файл (успели создать, но ещё не
            # записали) тоже разрешается повтором.
            continue
        return value
    raise RuntimeError(f"не удалось создать файл секрета: {path}")


class Settings(BaseSettings):
    """Настройки демона.

    Слушаем только петлевой интерфейс: API даёт полный контроль над аккаунтами,
    наружу он не выставляется.
    """

    model_config = SettingsConfigDict(env_prefix="MAXUB_", extra="ignore")

    data_dir: Path = Field(default=Path("/data"))
    host: str = Field(default="127.0.0.1")
    port: int = Field(default=8765)
    token: str | None = Field(default=None)
    secret_key: str | None = Field(default=None)
    transport: str = Field(default="stub")
    log_level: str = Field(default="info")

    # Где живёт ключ шифрования сессий. `auto` — в хранилище ОС, если оно есть,
    # иначе в файле; `file` — только файл, даже когда хранилище доступно;
    # `os` — только хранилище, отказ при его отсутствии (осознанное требование
    # привязки к машине, а не «как получится»).
    secret_key_store: Literal["auto", "os", "file"] = Field(default="auto")

    # Удалять ли `secret.key` после того, как ключ оказался в хранилище ОС.
    # Выключено намеренно, обоснование — в `keystore.resolve_key`.
    secret_key_drop_file: bool = Field(default=False)

    # Веб-панель управления аккаунтами. Включена по умолчанию: без неё аккаунт
    # добавляется только из CLI. Выключается тем, кому лишняя поверхность в
    # браузере не нужна — тогда маршрутов `/web/*` в приложении просто нет.
    web_ui: bool = Field(default=True)

    # Дополнительные имена, по которым разрешено открывать панель, через запятую.
    # Нужны, когда демон слушает 0.0.0.0 (штатно для проброса порта из Docker):
    # адрес привязки не говорит ничего о том, какому имени можно доверять, а
    # принимать любой заголовок Host — значит пустить чужой сайт, отрезолвленный
    # в 127.0.0.1, в один origin с панелью.
    web_allowed_hosts: str = Field(default="")

    # Повторы отправки. Задержка удваивается с каждой попыткой и переживает
    # перезапуск демона — иначе после рестарта всё ломится на сервер разом.
    retry_base_seconds: float = Field(default=5.0)
    retry_max_seconds: float = Field(default=600.0)
    max_send_attempts: int = Field(default=5)

    # Переподключение аккаунта после обрыва.
    reconnect_base_seconds: float = Field(default=3.0)
    reconnect_max_seconds: float = Field(default=300.0)

    # Лимиты. Значения консервативные, но это не «гарантированно безопасные»
    # пороги — таких для закрытого API не существует, см. docs/stack.md.
    send_rate_per_minute: float = Field(default=12.0)
    send_burst: int = Field(default=3)
    send_jitter_seconds: float = Field(default=1.5)

    @property
    def db_path(self) -> Path:
        return self.data_dir / DB_FILE

    @property
    def token_path(self) -> Path:
        return self.data_dir / TOKEN_FILE

    @property
    def secret_key_path(self) -> Path:
        return self.data_dir / KEY_FILE

    def ensure_data_dir(self) -> None:
        """Создаёт каталог данных с правами 0700.

        В каталоге лежат токен API и БД с сессиями аккаунтов — читать их не
        должен никто, кроме владельца процесса.
        """
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir.chmod(0o700)

    def resolve_token(self) -> str:
        """Возвращает токен API, создавая его при первом запуске.

        Токен кладётся в файл с правами 0600, чтобы клиент внутри того же
        контейнера мог его прочитать без ручной передачи.
        """
        if self.token:
            return self.token
        self.ensure_data_dir()
        return _create_or_read_secret(self.token_path, lambda: secrets.token_urlsafe(32))

    def resolve_secret_key(self) -> str:
        """Возвращает ключ шифрования сессий, создавая его при первом запуске.

        Источники перебираются от самого явного к самому доступному:

        1. `MAXUB_SECRET_KEY` — ключ вообще не касается диска и приходит извне,
           например из секретов оркестратора. Раз его задали явно, спорить с
           этим не должно ничто;
        2. хранилище ОС — Windows DPAPI или Linux Secret Service. Ключ читает
           только тот же пользователь на той же машине;
        3. файл `secret.key` рядом с базой — работает везде, в том числе в
           контейнере, где никакого хранилища ОС нет.

        Порядок такой, потому что каждый следующий источник шире предыдущего по
        охвату и слабее по защите. Отсутствие хранилища ОС — штатный случай:
        демон в Docker работает ровно как раньше, файлом.

        Про судьбу существующего `secret.key` при переходе на хранилище — в
        [resolve_key][maxub.core.keystore.resolve_key].
        """
        if self.secret_key:
            return self.secret_key
        self.ensure_data_dir()
        store = self._open_key_store()
        if store is not None:
            key = keystore.resolve_key(
                store,
                self.secret_key_path,
                drop_file=self.secret_key_drop_file,
                # Пустое хранилище — это либо первый запуск, либо потерянный
                # ключ. Отличить их можно только здесь: если база уже есть,
                # заводить новый ключ нельзя.
                may_create=not self.db_path.exists(),
            )
            if key is not None:
                return key
            if self.secret_key_store == "os":
                # Режим выбран явно: тихо съехать на файл — значит дать не ту
                # защиту, о которой попросили.
                raise RuntimeError(
                    "MAXUB_SECRET_KEY_STORE=os, но хранилище ключей ОС ответило отказом"
                )
        self._refuse_blind_new_key(store_failed=store is not None)
        return _create_or_read_secret(self.secret_key_path, generate_key)

    def _open_key_store(self) -> keystore.KeyStore | None:
        """Открывает хранилище ОС с учётом выбранного режима."""
        if self.secret_key_store == "file":
            return None
        store = open_keystore(self.data_dir)
        if store is None and self.secret_key_store == "os":
            raise RuntimeError(
                "MAXUB_SECRET_KEY_STORE=os, но хранилища ключей ОС нет:"
                " в Linux нужен запущенный Secret Service и установленный keyring"
            )
        return store

    def _refuse_blind_new_key(self, store_failed: bool) -> None:
        """Не даёт создать новый ключ там, где старый ещё может быть жив.

        Новый ключ поверх базы с сессиями — тихая потеря доступа ко всем
        аккаунтам, поэтому в двух случаях демон честно не стартует:

        - остался след переноса ключа в хранилище ОС, а самого ключа мы не
          получили: хранилище пропало вместе с библиотекой, сеансом или
          профилем пользователя, но ключ там;
        - хранилище было и ответило отказом, файла нет, а база уже есть.

        Случай «ни хранилища, ни следа» сюда не попадает намеренно: это обычный
        первый запуск в контейнере, и он должен работать как раньше.
        """
        if self.secret_key_path.exists():
            return
        marker = keystore.moved_marker(self.secret_key_path)
        if marker.exists():
            raise RuntimeError(
                "ключ шифрования перенесён в хранилище ОС, а получить его оттуда"
                " сейчас не удаётся: новый ключ сделал бы сессии нечитаемыми."
                " Восстановите доступ к хранилищу, задайте MAXUB_SECRET_KEY или"
                f" удалите {marker}, если сессии больше не нужны"
            )
        if store_failed and self.db_path.exists():
            raise RuntimeError(
                "хранилище ключей ОС недоступно, а файла secret.key нет,"
                " хотя база уже существует: новый ключ сделал бы сессии"
                " нечитаемыми. Задайте MAXUB_SECRET_KEY или восстановите файл"
            )


class ClientSettings(BaseSettings):
    """Настройки тонкого клиента (`maxub` / `maxubctl`)."""

    model_config = SettingsConfigDict(env_prefix="MAXUB_", extra="ignore")

    data_dir: Path = Field(default=Path("/data"))
    host: str = Field(default="127.0.0.1")
    port: int = Field(default=8765)
    token: str | None = Field(default=None)
    url: str | None = Field(default=None)
    timeout: float = Field(default=30.0)

    @property
    def base_url(self) -> str:
        return self.url or f"http://{self.host}:{self.port}"

    def resolve_token(self) -> str | None:
        if self.token:
            return self.token
        path = self.data_dir / TOKEN_FILE
        if path.exists():
            return path.read_text(encoding="utf-8").strip() or None
        return None
