"""Где лежат данные, если каталог не задан явно.

Модуль намеренно не зависит ни от чего внутри проекта: его зовут и настройки
демона, и обвязка Windows, а обратная зависимость (`config` знает про
`winhost`) означала бы, что конфигурация тащит за собой платформенную обвязку.

Раньше значением по умолчанию был `/data` — путь тома в контейнере. В
контейнере он и остаётся, но задаётся явно (`MAXUB_DATA_DIR` в образе), потому
что на обычной машине этот дефолт не работал вовсе: демон делает
`mkdir(parents=True)` и получает отказ доступа в корне. То есть колесо из
релиза без переменной окружения не запускалось — с этого и начата правка.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_DIR_NAME = "maxub"


class DataDirError(RuntimeError):
    """Каталог данных недоступен: нет прав, занят файлом, не создаётся.

    Отдельный тип, а не голый `RuntimeError`, потому что отказ обрабатывают три
    разных входа и каждый по-своему: CLI печатает сообщение и возвращает код,
    exe показывает окно (трассировке в собранном без консоли процессе выводиться
    некуда), демон в контейнере просто падает с текстом в журнале. Ловить ради
    этого любой `RuntimeError` значило бы глотать и чужие ошибки. Наследование
    от `RuntimeError` оставлено намеренно: код, ловивший его раньше, продолжает
    работать.
    """


def default_data_dir(platform: str | None = None) -> Path:
    """Каталог данных по умолчанию для текущей платформы.

    Windows — `%LOCALAPPDATA%\\maxub`. Выбран `Local`, а не `Roaming`:
    в каталоге лежат БД с сессиями и токен, их нельзя таскать за пользователем
    по перемещаемому профилю.

    macOS — `~/Library/Application Support/maxub`, там это штатное место для
    данных приложения.

    Остальное (Linux, BSD) — XDG: `$XDG_DATA_HOME/maxub`, иначе
    `~/.local/share/maxub`.

    Аргумент `platform` нужен тестам: перебрать ветки, не подменяя `sys`.
    """
    system = platform if platform is not None else sys.platform

    if system == "win32":
        # Относительный путь отбрасывается по той же причине, что и в ветке XDG
        # ниже: данные должны лежать в одном месте независимо от того, откуда
        # запустили программу.
        local = os.environ.get("LOCALAPPDATA")
        if local and Path(local).is_absolute():
            return Path(local) / APP_DIR_NAME
        # Переменной нет — так бывает в служебных сеансах и под Wine.
        return Path.home() / "AppData" / "Local" / APP_DIR_NAME

    if system == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_DIR_NAME

    # Спецификация XDG требует игнорировать относительный путь, а не склеивать
    # его с текущим каталогом: иначе данные окажутся там, откуда запустили, и
    # при следующем запуске из другого места демон их «потеряет».
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        candidate = Path(xdg)
        if candidate.is_absolute():
            return candidate / APP_DIR_NAME

    return Path.home() / ".local" / "share" / APP_DIR_NAME
