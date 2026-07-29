"""Проверяет, что библиотека транспорта попала в собранный exe целиком.

Успешная сборка сама по себе ничего не доказывает: exe без транспорта выглядит
ровно так же и весит почти столько же, а упрётся в `ModuleNotFoundError` только
у пользователя, выбравшего `MAXUB_TRANSPORT=pymax`, — то есть там, где доложить
недостающее нечем. Отказ спеки (`SystemExit`) закрывает лишь случай «библиотеки
в окружении сборки нет вовсе»; здесь проверяется то, что важно на самом деле, —
что в архив попал ВЕСЬ пакет, а не сколько-то его модулей.

Сравнение с установленным пакетом, а не со списком имён: список устарел бы при
первом же обновлении библиотеки и начал бы врать в обратную сторону — молча
пропускать пропажу новых модулей.

Отдельный файл, а не heredoc в workflow. Раньше проверка жила прямо в
`.github/workflows/windows-build.yml` и потому выполнялась ровно в одном месте —
на раннере GitHub. Локальная сборка под Wine (`build.sh`) её не выполняла вовсе,
и разошлись они немедленно: скрипт падал на каждом прогоне, а локально сборка
считалась проверенной. Теперь один и тот же файл зовут оба пути.

Запускается тем же интерпретатором, которым собран exe: список модулей берётся
из установленного пакета, и чужое окружение дало бы другой список.
"""

from __future__ import annotations

import importlib
import os
import pkgutil
import sys
import tempfile

from PyInstaller.archive.readers import CArchiveReader, ZlibArchiveReader

PACKAGE = "pymax"


def force_utf8_output() -> None:
    """Переводит вывод в UTF-8, не давая ему упасть на непредставимом символе.

    Ровно та же беда, что у консольного exe (см. `entry/maxubctl_console.py`), и
    ровно то же лечение. Сообщения здесь русские, а Windows-Python, чей вывод
    уходит в пайп, берёт кодировку из ANSI-кодовой страницы системы. У раннера
    GitHub это cp1252, куда кириллица не переводится вовсе, — и проверка
    заканчивалась `UnicodeEncodeError` вместо ответа. Причём падала она на
    строке об успехе: сама сборка была в порядке, а прогон — красным.

    `errors="replace"` — на случай кодовой страницы, где непредставим уже
    какой-нибудь отдельный символ: увидеть текст с испорченным знаком лучше, чем
    не увидеть ничего.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def installed_modules() -> set[str]:
    """Имена модулей библиотеки транспорта в окружении сборки."""
    module = importlib.import_module(PACKAGE)
    return {PACKAGE} | {
        name for _, name, _ in pkgutil.walk_packages(module.__path__, PACKAGE + ".")
    }


def bundled_modules(exe: str) -> set[str]:
    """Имена модулей внутри exe.

    Чистый Python лежит не в самом архиве exe, а во вложенном PYZ: искать
    `pymax` в `CArchiveReader.toc` бесполезно, там только бинарники и данные.
    """
    reader = CArchiveReader(exe)
    names = [name for name in reader.toc if name.lower().endswith(".pyz")]
    if not names:
        # Иначе это был бы `StopIteration` без единого слова о том, что за файл
        # подсунули проверке.
        raise SystemExit(f"{exe}: внутри нет PYZ — это точно сборка PyInstaller?")
    # Отдельный каталог на каждый вызов, а не одно имя в общем temp: постоянное
    # имя в общем каталоге — это и права соседа по машине, и два параллельных
    # прогона, пишущих в один файл.
    with tempfile.TemporaryDirectory() as directory:
        path = os.path.join(directory, "bundle.pyz")
        with open(path, "wb") as handle:
            handle.write(reader.extract(names[0]))
        archive = ZlibArchiveReader(path)
        modules = set(archive.toc)
        # Ссылка отпускается до выхода из блока: Windows не даёт удалить файл,
        # который кто-то ещё держит открытым, и уборка каталога упала бы.
        del archive
    return modules


def main(argv: list[str]) -> int:
    force_utf8_output()
    if not argv:
        print("usage: check_bundle.py <exe> [<exe> ...]", file=sys.stderr)
        return 2
    on_disk = installed_modules()
    for exe in argv:
        missing = sorted(on_disk - bundled_modules(exe))
        if missing:
            print(f"{exe}: в сборке не хватает модулей {PACKAGE}: {missing}", file=sys.stderr)
            return 1
        print(f"{exe}: {PACKAGE} целиком, {len(on_disk)} модулей")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
