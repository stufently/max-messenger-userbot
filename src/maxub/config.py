"""Конфигурация демона и клиентов. Читается из переменных окружения MAXUB_*."""

from __future__ import annotations

import os
import secrets
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from maxub.core.crypto import generate_key

TOKEN_FILE = "api_token"
KEY_FILE = "secret.key"
DB_FILE = "maxub.db"


def _write_secret_file(path: Path, content: str) -> None:
    """Создаёт файл сразу с правами 0600.

    Вариант «записать, потом chmod» оставляет окно, в котором файл доступен по
    umask, — для секретов это неприемлемо.
    """
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, content.encode("utf-8"))
    finally:
        os.close(fd)


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
        path = self.token_path
        if path.exists():
            existing = path.read_text(encoding="utf-8").strip()
            if existing:
                return existing
        token = secrets.token_urlsafe(32)
        _write_secret_file(path, token)
        return token

    def resolve_secret_key(self) -> str:
        """Возвращает ключ шифрования сессий, создавая его при первом запуске.

        Приоритет у переменной окружения: она позволяет держать ключ вне тома с
        базой — например, в секретах оркестратора.
        """
        if self.secret_key:
            return self.secret_key
        self.ensure_data_dir()
        path = self.secret_key_path
        if path.exists():
            existing = path.read_text(encoding="utf-8").strip()
            if existing:
                return existing
        key = generate_key()
        _write_secret_file(path, key)
        return key


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
