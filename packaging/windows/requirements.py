"""Выписывает зависимости проекта из pyproject.toml в requirements.txt.

Запускается Windows-интерпретатором внутри сборочного образа: другого Python
там нет, а `pip install .` не подходит — исходников в образе нет, монтируются
они только на сборку. Так список зависимостей остаётся в одном месте.
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

# Необязательные группы, которые всё равно должны попасть в exe.
#
# В pyproject `pymax` объявлен extra потому, что установленному из PyPI пакету
# библиотека транспорта не обязательна: без неё платформа работает на заглушке.
# В one-file exe эта логика ломается — доставить туда пакет нечем, pip внутрь
# собранного файла не ходит. То есть «необязательная» зависимость в exe либо
# лежит с самого начала, либо не появится никогда, и выбор `MAXUB_TRANSPORT=pymax`
# упирается в невыполнимый совет установить её.
#
# `keyring` сюда не входит намеренно: он нужен для Secret Service (GNOME
# Keyring, KWallet), а под Windows ключ и так защищается DPAPI через ctypes.
BUNDLED_EXTRAS = ("pymax",)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: requirements.py <pyproject.toml> <requirements.txt>", file=sys.stderr)
        return 2
    source, target = Path(argv[0]), Path(argv[1])
    data = tomllib.loads(source.read_text(encoding="utf-8"))
    project = data["project"]
    deps: list[str] = list(project["dependencies"])
    extras = project.get("optional-dependencies", {})
    for name in BUNDLED_EXTRAS:
        # Опечатка в имени группы иначе прошла бы молча: список просто оказался
        # бы короче, а транспорт не попал бы в exe — ровно та поломка, ради
        # которой этот список и заведён.
        if name not in extras:
            print(f"requirements.py: в pyproject нет группы {name!r}", file=sys.stderr)
            return 1
        deps += list(extras[name])
    target.write_text("\n".join(deps) + "\n", encoding="utf-8")
    print("\n".join(deps))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
