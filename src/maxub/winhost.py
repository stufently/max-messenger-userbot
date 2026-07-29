"""Обвязка Windows для автономной сборки.

Всё, что зависит от особенностей системы, а не от логики запуска: каталог
данных, свободный порт, журнал, окно с ошибкой, обращения к демону по HTTP,
защита от второго экземпляра и файл с адресом работающего демона.

Отделено от [winlauncher][maxub.winlauncher] намеренно: там читается сценарий
«поднять демон и открыть панель», здесь — платформенные подробности, которые
этот сценарий только загромождают. Ни один `print` тут не годится: у собранного
без консоли exe stdout некуда выводить.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import IO

from maxub.config import Settings

APP_DIR_NAME = "maxub"
RUNTIME_FILE = "runtime.json"
INSTANCE_LOCK_FILE = "instance.lock"
LOG_FILE = "launcher.log"

log = logging.getLogger(__name__)


def default_data_dir() -> Path:
    """Каталог данных: `%LOCALAPPDATA%\\maxub`.

    Значение по умолчанию из `Settings` (`/data`) — это путь тома в контейнере,
    в Windows его писать некуда. `LOCALAPPDATA` выбран вместо `Roaming`
    осознанно: в каталоге лежат БД с сессиями и токен, их нельзя таскать за
    пользователем по перемещаемому профилю.

    Про права. `Settings.ensure_data_dir` делает `chmod 0700`; в Windows это
    меняет только атрибут «только для чтения» и никого не ограничивает. Защита
    там держится на ACL профиля: каталог наследует права `%LOCALAPPDATA%`, где
    доступ есть у владельца, SYSTEM и администраторов. Отдельно ужесточать ACL
    смысла нет — администратор и так читает чужие профили.
    """
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return Path(local) / APP_DIR_NAME
    if sys.platform == "win32":
        return Path.home() / "AppData" / "Local" / APP_DIR_NAME
    # Не-Windows встречается только при отладке сборки (например, под Wine в
    # контейнере, где LOCALAPPDATA может быть не задан).
    return Path.home() / f".{APP_DIR_NAME}"


def pick_free_port() -> int:
    """Просит систему выдать свободный порт на петле.

    Фиксированный 8765 занят, если рядом работает демон из Docker или второй
    экземпляр. Между освобождением сокета и его захватом uvicorn остаётся узкое
    окно гонки; передать готовый сокет внутрь `daemon.serve` нельзя, не меняя
    его сигнатуру, а цена промаха — понятная ошибка и повторный запуск.
    """
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def configure_logging(settings: Settings) -> None:
    """Журнал в файл: у собранного без консоли exe другого места нет."""
    logging.basicConfig(
        filename=str(settings.data_dir / LOG_FILE),
        filemode="a",
        # Явный utf-8: иначе на русской Windows журнал пишется в кодировке
        # системы, а непредставимые символы превращаются в \uXXXX.
        encoding="utf-8",
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        force=True,
    )


def show_error(message: str) -> None:
    """Сообщает об ошибке окном — иначе запуск двойным щелчком молча умирает."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, message, "MAX Userbot", 0x10)
    except Exception:  # noqa: BLE001 — окно необязательно, журнал уже написан
        log.exception("не удалось показать окно с ошибкой")


def request_handoff_code(
    base_url: str, handoff_path: str, token: str, timeout: float = 5.0
) -> str | None:
    """Просит у демона одноразовый код входа в панель.

    Токен уходит в заголовке `Authorization` на петлевой адрес и дальше этого
    процесса не идёт: в браузер попадает только код, который гаснет при первом
    же переходе. Так лаунчер открывает панель сразу входом, не заставляя
    человека переносить туда сам токен.

    `None` — код не выдан (панель выключена настройкой, демон отвечает иначе
    или сеть подвела). Это не повод отказываться открывать панель: вход по
    токену в форму остаётся рабочим, поэтому ошибка только пишется в журнал.
    """
    request = urllib.request.Request(
        f"{base_url}{handoff_path}",
        method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Length": "0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        code = payload["code"]
    except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError):
        log.warning("демон не выдал одноразовый код входа", exc_info=True)
        return None
    # Ответ мог прийти не от нашего демона (порт занят чем-то другим), поэтому
    # тип проверяется явно: подставлять в адрес что попало незачем.
    return code if isinstance(code, str) and code else None


def probe_health(base_url: str, health_path: str, timeout: float = 1.0) -> bool:
    """Проверяет живость демона маршрутом, не требующим токена."""
    try:
        with urllib.request.urlopen(f"{base_url}{health_path}", timeout=timeout) as response:
            return bool(response.status == 200)
    except (urllib.error.URLError, OSError, ValueError):
        return False


# Файл замка держится открытым до конца процесса: блокировка живёт, пока живёт
# дескриптор. Положить его в локальную переменную значило бы отпустить защиту
# сразу после возврата из функции. Система снимает блокировку сама при выходе
# процесса — устаревших замков после падения не остаётся.
_instance_lock: IO[bytes] | None = None


def acquire_single_instance(data_dir: Path) -> bool:
    """Пытается стать единственным экземпляром. `False` — каталог уже занят.

    Одного `runtime.json` мало: файл появляется только после того, как демон
    ответил на проверку живости, а два быстрых двойных щелчка укладываются в это
    окно. Две копии на одной БД — это две очереди отправки и два соединения
    одного аккаунта, то есть разъезжающееся состояние.

    Замок именно на файле внутри каталога данных, а не именованный мьютекс.
    Мьютекс в области `Local\\` разделён по сеансам Windows: один и тот же
    пользователь, зашедший ещё и по RDP, поднял бы вторую копию на той же базе.
    Блокировка файла привязана к тому, что мы и защищаем, — к каталогу данных, —
    и потому работает и между сеансами, и когда каталог задан через
    `MAXUB_DATA_DIR`.

    Отказ трактуется как «занято»: продолжить, не зная наверняка, значит
    допустить две копии на одной базе — а это хуже, чем не запуститься.
    """
    global _instance_lock
    path = data_dir / INSTANCE_LOCK_FILE
    try:
        handle = path.open("a+b")
    except OSError:
        log.exception("не удалось открыть файл замка %s", path)
        return False
    try:
        handle.seek(0)
        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return False
    _instance_lock = handle
    return True


def write_runtime(data_dir: Path, base_url: str) -> None:
    """Публикует адрес демона для второго запуска и для `maxubctl`.

    Через временный файл и `os.replace`: читатель никогда не должен наткнуться
    на половину записи.
    """
    payload = json.dumps({"url": base_url, "pid": os.getpid()})
    tmp = data_dir / f"{RUNTIME_FILE}.tmp"
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, data_dir / RUNTIME_FILE)


def drop_runtime(data_dir: Path, base_url: str) -> None:
    """Убирает файл, только если он описывает именно этот процесс.

    Иначе завершившийся экземпляр стирал бы адрес чужого, живого демона.
    """
    path = data_dir / RUNTIME_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if data.get("url") == base_url:
        path.unlink(missing_ok=True)


def is_local_daemon_url(url: str) -> bool:
    """Проверяет, что адрес указывает на демон на этой же машине.

    Из `runtime.json` адрес попадает в `MAXUB_URL`, а клиент шлёт туда bearer-
    токен. Файл лежит в каталоге данных, но каталог задаётся переменной
    окружения и может оказаться в общем месте с небрежными правами — тогда
    подменённый адрес увёл бы токен на чужой сервер. Поэтому доверяем не факту
    ответа на проверку живости, а самому адресу: только `http` на петлевой
    адрес, без учётных данных, пути, запроса и якоря.
    """
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "http" or parsed.path or parsed.query or parsed.fragment:
        return False
    if parsed.username or parsed.password:
        return False
    host = (parsed.hostname or "").lower()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def running_instance(data_dir: Path, health_path: str) -> str | None:
    """Адрес уже запущенного экземпляра, если он отвечает.

    Второй демон на той же БД — это две очереди отправки и два соединения с
    одного аккаунта, поэтому повторный запуск должен открывать браузер, а не
    поднимать копию. Файл может остаться от аварийно завершённого процесса,
    поэтому верим не файлу, а ответу на проверку живости — но только после
    того, как убедились, что адрес вообще местный.
    """
    path = data_dir / RUNTIME_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        url = str(data["url"])
    except (OSError, ValueError, KeyError):
        return None
    if not is_local_daemon_url(url):
        log.warning("адрес из %s не является локальным, игнорирую: %r", RUNTIME_FILE, url)
        return None
    return url if probe_health(url, health_path) else None
