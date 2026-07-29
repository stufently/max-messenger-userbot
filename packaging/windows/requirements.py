"""Выписывает зависимости проекта из pyproject.toml в requirements.txt.

Запускается Windows-интерпретатором внутри сборочного образа: другого Python
там нет, а `pip install .` не подходит — исходников в образе нет, монтируются
они только на сборку. Так список зависимостей остаётся в одном месте.
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: requirements.py <pyproject.toml> <requirements.txt>", file=sys.stderr)
        return 2
    source, target = Path(argv[0]), Path(argv[1])
    data = tomllib.loads(source.read_text(encoding="utf-8"))
    deps: list[str] = list(data["project"]["dependencies"])
    target.write_text("\n".join(deps) + "\n", encoding="utf-8")
    print("\n".join(deps))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
