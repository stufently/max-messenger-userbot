"""Скрипт запуска оконной сборки `maxub.exe`.

Отдельный файл, а не `src/maxub/winlauncher.py` напрямую: PyInstaller
запускает свой скрипт как `__main__`, и, если бы им был модуль пакета, тот же
код оказался бы в сборке дважды — как `__main__` и как `maxub.winlauncher`.
Здесь тонкая обёртка, вся логика — в модуле.
"""

from __future__ import annotations

import sys

from maxub.winlauncher import main

if __name__ == "__main__":
    sys.exit(main())
