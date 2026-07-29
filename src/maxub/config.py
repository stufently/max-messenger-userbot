"""Конфигурация демона и клиентов. Читается из переменных окружения MAXUB_*."""

from __future__ import annotations

import os
import secrets
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

TOKEN_FILE = "api_token"
DB_FILE = "maxub.db"


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
    transport: str = Field(default="stub")
    log_level: str = Field(default="info")

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
        # Файл создаётся сразу с правами 0600: вариант «записать, потом chmod»
        # оставляет окно, в котором файл доступен по umask.
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        try:
            os.write(fd, token.encode("utf-8"))
        finally:
            os.close(fd)
        return token


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
