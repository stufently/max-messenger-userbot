"""Скрипт запуска консольной сборки `maxubctl.exe`.

Зачем вторая сборка, если пользователю обещан один файл. `maxub.exe` собран без
консоли: у оконного процесса Windows нет ни stdout, ни stderr, и любая попытка
в них писать заканчивается «Invalid handle». Для `maxubctl` — машинного клиента
для скриптов и агентов — вывод и есть весь смысл, поэтому он собирается
отдельным консольным exe. Обычному пользователю он не нужен и в релизе идёт
рядом как дополнительный файл, а не как замена основному.

Каталог данных клиент выбирает сам и в Windows приходит туда же, куда лаунчер
(`%LOCALAPPDATA%\\maxub`), — здесь он подставляется в окружение, чтобы попасть и
в дочерние процессы. А вот порт по умолчанию — 8765, тогда как лаунчер берёт
свободный, поэтому адрес работающего демона читается из его файла состояния.
Именно `setdefault`: заданные пользователем `MAXUB_*` остаются главнее.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from maxub.cli.main import ctl
from maxub.paths import default_data_dir
from maxub.winhost import running_instance
from maxub.winlauncher import HEALTH_PATH


def _force_utf8_output() -> None:
    """Переводит вывод в UTF-8, не давая ему упасть на непредставимом символе.

    Весь текст программы русский, а консоль Windows по умолчанию работает в
    однобайтовой кодировке (cp866). Часть символов — рамки таблиц, тире, кавычки
    — в неё не переводится, и печать справки заканчивается `UnicodeEncodeError`
    вместо вывода. `errors="replace"` тут уместнее аккуратности: увидеть текст с
    испорченным символом лучше, чем не увидеть ничего.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def _apply_windows_defaults() -> None:
    # Каталог из окружения главнее вычисленного: им пользуются и лаунчер, и
    # тот, кто держит данные не в профиле.
    configured = os.environ.get("MAXUB_DATA_DIR")
    data_dir = Path(configured) if configured else default_data_dir()
    os.environ.setdefault("MAXUB_DATA_DIR", str(data_dir))
    url = running_instance(data_dir, HEALTH_PATH)
    if url:
        os.environ.setdefault("MAXUB_URL", url)


if __name__ == "__main__":
    _force_utf8_output()
    _apply_windows_defaults()
    ctl()
