"""Хранилища ключа, зависящие от платформы.

Отделено от [keystore][maxub.core.keystore] намеренно: там политика — какой
источник ключа главнее, что делать при отказе, как согласовать хранилище с
файлом. Здесь — вызовы конкретных систем, которые в Linux-контейнере CI
выполнить нельзя вовсе. Граница проходит по протоколу
[KeyStore][maxub.core.keystore.KeyStore]: политика проверяется на подменённом
хранилище, платформенная часть остаётся тонкой и без решений.
"""

from __future__ import annotations

import ctypes
import importlib
import logging
import sys
from pathlib import Path
from typing import Any

from maxub.core.keystore import KeyStore, KeyStoreError, write_blob

log = logging.getLogger(__name__)

#: Имя службы в Secret Service. Учётной записью служит путь к каталогу данных:
#: ключ привязан к конкретной базе, иначе два демона на одной машине затирали
#: бы ключи друг друга.
KEYRING_SERVICE = "maxub"
#: Файл с DPAPI-конвертом ключа. Расширение отличается от `secret.key`, чтобы
#: по одному имени было видно: это не ключ открытым текстом.
DPAPI_KEY_FILE = "secret.key.dpapi"
#: Бэкенды `keyring`, которые действительно кладут ключ в хранилище ОС. Любой
#: другой — файловые из `keyrings.alt`, заглушка `fail`, сторонние плагины —
#: даёт не ту защиту, которую обещает режим `os`, и за хранилище не считается.
SUPPORTED_BACKENDS = (
    "keyring.backends.SecretService",
    "keyring.backends.kwallet",
    "keyring.backends.libsecret",
    "keyring.backends.macOS",
)
#: CRYPTPROTECT_UI_FORBIDDEN. Демону нельзя показывать диалоги: он может
#: работать без сеанса, и окно просто повесило бы запуск.
_UI_FORBIDDEN = 0x01


def _windll(name: str) -> Any:
    """Загружает системную библиотеку Windows.

    Через `getattr`, а не прямой ссылкой на `ctypes.WinDLL`: атрибута нет на
    других платформах, а модуль обязан импортироваться везде — на Linux он
    просто не используется.
    """
    loader = getattr(ctypes, "WinDLL", None)
    if loader is None:
        raise KeyStoreError("DPAPI доступен только в Windows")
    return loader(name, use_last_error=True)


def _last_error() -> int:
    """Код последней ошибки WinAPI.

    Тоже через `getattr`: `ctypes.get_last_error` существует только там, где
    есть WinAPI, а без кода ошибки сообщение всё равно останется осмысленным.
    """
    return int(getattr(ctypes, "get_last_error", lambda: 0)())


def _local_free(pointer: Any) -> None:
    """Отдаёт системе буфер, выделенный DPAPI.

    Освобождает его вызывающий — иначе расшифрованный ключ остаётся в куче
    процесса дольше, чем нужно, да и память течёт.
    """
    local_free = _windll("kernel32").LocalFree
    local_free.argtypes = (ctypes.c_void_p,)
    local_free.restype = ctypes.c_void_p
    local_free(ctypes.cast(pointer, ctypes.c_void_p))


class _DataBlob(ctypes.Structure):
    """`DATA_BLOB` из wincrypt.h.

    Поле длины описано через `c_uint32`, а не через `wintypes.DWORD`: импорт
    `ctypes.wintypes` падает вне Windows, а размер у DWORD и так фиксированный.
    """

    _fields_ = (("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_char)))


class DpapiKeyStore:
    """Ключ на диске, зашифрованный DPAPI под текущего пользователя.

    Дополнительная энтропия не передаётся намеренно: в открытом исходнике она
    была бы константой, то есть не секретом, и создавала бы видимость защиты,
    которой нет. Реальная привязка здесь — учётная запись Windows.
    """

    name = "dpapi"

    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self) -> str | None:
        if not self._path.exists():
            return None
        return self._crypt("CryptUnprotectData", self._path.read_bytes()).decode("ascii")

    def save(self, key: str) -> None:
        write_blob(self._path, self._crypt("CryptProtectData", key.encode("ascii")))

    def _crypt(self, function: str, data: bytes) -> bytes:
        entry = getattr(_windll("crypt32"), function)
        # Сигнатуру задаём явно: без неё ctypes догадывается о типах сам, а на
        # 64 битах такая догадка молча урезает указатели до int.
        entry.argtypes = (
            ctypes.POINTER(_DataBlob),
            ctypes.c_wchar_p,
            ctypes.POINTER(_DataBlob),
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.POINTER(_DataBlob),
        )
        entry.restype = ctypes.c_int
        buffer = ctypes.create_string_buffer(data, len(data))
        source = _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
        result = _DataBlob()
        ok = entry(
            ctypes.byref(source), None, None, None, None, _UI_FORBIDDEN, ctypes.byref(result)
        )
        if not ok:
            raise KeyStoreError(f"{function} отказал, код {_last_error()}")
        try:
            return ctypes.string_at(result.pbData, result.cbData)
        finally:
            _local_free(result.pbData)


class SecretServiceKeyStore:
    """Ключ в Secret Service через библиотеку `keyring`."""

    name = "secret-service"

    def __init__(self, account: str, module: Any) -> None:
        self._account = account
        self._module = module

    def load(self) -> str | None:
        value = self._call(self._module.get_password, KEYRING_SERVICE, self._account)
        return str(value) if value else None

    def save(self, key: str) -> None:
        self._call(self._module.set_password, KEYRING_SERVICE, self._account, key)

    def _call(self, method: Any, *args: str) -> Any:
        try:
            return method(*args)
        except Exception as exc:
            # Ловим широко осознанно: `keyring` пропускает наружу ошибки
            # конкретного бэкенда — заблокированную связку ключей, обрыв
            # D-Bus, — и общего класса у них нет.
            raise KeyStoreError(f"Secret Service отказал: {exc}") from exc


def open_keystore(data_dir: Path, platform: str | None = None) -> KeyStore | None:
    """Подбирает хранилище под платформу. `None` — хранилища нет.

    Возвращать `None` вместо исключения принципиально: контейнер без сеансового
    демона и сборка без необязательной зависимости — обычная жизнь, а не сбой.
    """
    system = platform if platform is not None else sys.platform
    if system == "win32":
        return DpapiKeyStore(data_dir / DPAPI_KEY_FILE)
    module = _load_keyring()
    if module is None:
        return None
    return SecretServiceKeyStore(str(data_dir), module)


def _load_keyring() -> Any | None:
    """Мягкий импорт `keyring` с проверкой, что за ним настоящее хранилище ОС.

    Импорт через `importlib`: библиотека необязательная, прямой `import` в
    заголовке модуля сделал бы её обязательной для всех, включая контейнер, где
    хранилища всё равно нет.

    Мало убедиться, что бэкенд не заглушка: `keyring` умеет писать и в обычный
    файл (`keyrings.alt`), а это ровно та защита, от которой мы уходим. Поэтому
    признаём только перечисленные бэкенды — по имени модуля, чтобы не тащить
    приватные импорты ради одной проверки.
    """
    try:
        module = importlib.import_module("keyring")
        backend = module.get_keyring()
    except Exception:
        # Библиотеки нет либо она не смогла собрать список бэкендов — для нас
        # это одно и то же: хранилища нет.
        return None
    origin = type(backend).__module__
    if not origin.startswith(SUPPORTED_BACKENDS):
        log.debug("бэкенд keyring %s не считается хранилищем ОС", origin)
        return None
    return module
