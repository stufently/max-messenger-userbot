"""Скрипт запуска консольной сборки `maxubctl.exe`.

Зачем вторая сборка, если пользователю обещан один файл. `maxub.exe` собран без
консоли: у оконного процесса Windows нет ни stdout, ни stderr, и любая попытка
в них писать заканчивается «Invalid handle». Для `maxubctl` — машинного клиента
для скриптов и агентов — вывод и есть весь смысл, поэтому он собирается
отдельным консольным exe. Обычному пользователю он не нужен и в релизе идёт
рядом как дополнительный файл, а не как замена основному.

Клиент по умолчанию ищет токен в `/data` и стучится на 127.0.0.1:8765 — это
значения для контейнера. В Windows каталог другой, а порт лаунчер выбирает
свободный, поэтому здесь подставляются значения текущего запуска. Именно
`setdefault`: заданные пользователем `MAXUB_*` остаются главнее.
"""

from __future__ import annotations

import os
from pathlib import Path

from maxub.cli.main import ctl
from maxub.winhost import default_data_dir, running_instance
from maxub.winlauncher import HEALTH_PATH


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
    _apply_windows_defaults()
    ctl()
