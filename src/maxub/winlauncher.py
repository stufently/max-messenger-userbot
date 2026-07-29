"""Точка входа автономной сборки под Windows.

Пользователь запускает один `maxub.exe` двойным щелчком: демон поднимается на
петлевом интерфейсе, браузер открывается на панели управления. Ни установки
Python, ни командной строки — поэтому здесь нет ни одного `print`: у собранного
без консоли exe stdout некуда выводить, диагностика идёт в файл журнала и, для
фатальных ошибок, в окно сообщения.

Модуль сознательно не трогает `daemon.serve` и `Settings`: всё, что специфично
для Windows (каталог данных, свободный порт, браузер), настраивается снаружи —
ядро остаётся одинаковым для Docker и для exe. Платформенные подробности живут
в [winhost][maxub.winhost], здесь — только сценарий запуска.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
import time
import urllib.parse
import webbrowser

from maxub.config import Settings
from maxub.daemon import serve
from maxub.winhost import (
    acquire_single_instance,
    configure_logging,
    default_data_dir,
    drop_runtime,
    pick_free_port,
    probe_health,
    request_handoff_code,
    running_instance,
    show_error,
    write_runtime,
)

# Первый запуск создаёт БД и ключ шифрования, поэтому ожидание щедрое: лучше
# подождать, чем открыть браузер на ещё не поднятом порту.
STARTUP_TIMEOUT = 45.0

log = logging.getLogger(__name__)


# --- стык с веб-интерфейсом: СВЕРИТЬ С api/routes/web.py ----------------------
# Здесь собрано всё, что зависит от чужого модуля, чтобы его правка сводилась к
# одному месту в лаунчере.
#
# На момент написания: страница отдаётся по `GET /web`, обладатель токена демона
# получает одноразовый код входа по `POST /web/handoff`, а переход на
# `GET /web/enter?code=...` меняет код на сессию браузера и перенаправляет на
# саму страницу (см. web_handoff.py). Сам токен в браузер не передаётся ни через
# адрес, ни через буфер обмена: код живёт минуты и гаснет при первом же
# переходе, а токен — бессрочно.
#
# Если демон код не выдал (панель выключена настройкой, версии разошлись),
# открывается просто `/web`: вход по токену в форму никуда не делся.
#
# `require_local_host` в панели требует петлевой `Host`; лаунчер открывает
# 127.0.0.1, так что это условие выполняется.
WEB_PAGE_PATH = "/web"
WEB_ENTER_PATH = "/web/enter"
HANDOFF_PATH = "/web/handoff"
HEALTH_PATH = "/health"
CODE_QUERY_PARAM = "code"


def web_url(base_url: str, code: str | None = None) -> str:
    """Адрес панели, который открывается в браузере."""
    if code:
        query = urllib.parse.urlencode({CODE_QUERY_PARAM: code})
        return f"{base_url}{WEB_ENTER_PATH}?{query}"
    return f"{base_url}{WEB_PAGE_PATH}"


def panel_url(base_url: str, token: str) -> str:
    """Адрес панели с одноразовым кодом входа, если демон его выдал."""
    return web_url(base_url, request_handoff_code(base_url, HANDOFF_PATH, token))


def build_settings() -> Settings:
    """Настройки для запуска из exe.

    Переменные `MAXUB_*` не перетираются: они позволяют запустить ту же сборку
    с чужим каталогом данных или на фиксированном порту.
    """
    settings = Settings()
    if "MAXUB_DATA_DIR" not in os.environ:
        settings.data_dir = default_data_dir()
    if "MAXUB_PORT" not in os.environ:
        settings.port = pick_free_port()
    if "MAXUB_HOST" not in os.environ:
        settings.host = "127.0.0.1"
    return settings


def _announce_when_ready(settings: Settings, base_url: str, token: str) -> None:
    """Ждёт готовности демона и открывает браузер.

    Отдельный поток нужен потому, что `serve` блокирует основной до остановки.
    Ошибку внутри потока перехватываем целиком: демон при этом работает, и
    молча оборвавшийся поток оставил бы пользователя перед пустым экраном без
    единой подсказки, где искать причину.
    """
    try:
        _open_panel(settings, base_url, token)
    except Exception as exc:  # noqa: BLE001 — поток не должен умирать молча
        log.exception("не удалось открыть панель")
        show_error(f"Демон запущен, но панель открыть не удалось:\n{exc}")


def _open_panel(settings: Settings, base_url: str, token: str) -> None:
    deadline = time.monotonic() + STARTUP_TIMEOUT
    while time.monotonic() < deadline:
        if probe_health(base_url, HEALTH_PATH):
            break
        time.sleep(0.25)
    else:
        log.error("демон не ответил на %s за %.0f с", HEALTH_PATH, STARTUP_TIMEOUT)
        show_error("Демон не запустился. Подробности — в файле launcher.log.")
        return
    write_runtime(settings.data_dir, base_url)
    # В журнал идёт адрес без кода: файл журнала переживает сессию, а код —
    # это готовый вход в панель, пусть и на две минуты.
    log.info("панель: %s%s", base_url, WEB_PAGE_PATH)
    webbrowser.open(panel_url(base_url, token))


def main() -> int:
    """Запускает демон и панель; возвращает код выхода процесса."""
    settings = build_settings()
    try:
        settings.ensure_data_dir()
        configure_logging(settings)
    except OSError as exc:
        show_error(f"Не удалось подготовить каталог данных:\n{exc}")
        return 1

    existing = running_instance(settings.data_dir, HEALTH_PATH)
    if existing:
        log.info("экземпляр уже работает на %s, открываю панель", existing)
        token = settings.resolve_token()
        webbrowser.open(panel_url(existing, token))
        return 0
    # Замок берётся до создания секретов: два первых запуска иначе оба увидели
    # бы пустой каталог и полезли создавать токен наперегонки.
    if not acquire_single_instance(settings.data_dir):
        # Первый экземпляр ещё поднимается и адрес не опубликовал: открывать
        # нечего, а поднимать второго демона на той же базе нельзя.
        log.info("другой экземпляр уже занял каталог данных")
        show_error("MAX Userbot уже запускается. Подождите несколько секунд.")
        return 0

    try:
        token = settings.resolve_token()
    except OSError as exc:
        show_error(f"Не удалось подготовить токен доступа:\n{exc}")
        return 1

    base_url = f"http://{settings.host}:{settings.port}"
    threading.Thread(
        target=_announce_when_ready, args=(settings, base_url, token), daemon=True
    ).start()
    try:
        asyncio.run(serve(settings))
    except KeyboardInterrupt:
        return 0
    except Exception as exc:  # noqa: BLE001 — окно с ошибкой вместо тихой смерти
        log.exception("демон завершился с ошибкой")
        show_error(f"Демон завершился с ошибкой:\n{exc}\n\nПодробности — в launcher.log.")
        return 1
    finally:
        drop_runtime(settings.data_dir, base_url)
    return 0


if __name__ == "__main__":
    sys.exit(main())
