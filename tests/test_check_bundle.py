"""Проверка полноты транспорта в собранном exe: коды возврата и разбор архива.

Сам скрипт живёт в `packaging/`, а не в пакете `maxub`: он часть сборки, а не
программы. Отсюда две особенности. Загружается он по пути — импортировать
`packaging.windows.check_bundle` неоткуда, это не пакет. И читатели архивов
PyInstaller подменяются заглушками: PyInstaller ставится только в сборочное
окружение, а тесты гоняются там, где его нет.

Заглушки не делают проверку бессмысленной: разбирается здесь не чужой формат, а
собственное поведение скрипта — что он считает недостачей, чем отвечает на неё и
чем на исправную сборку.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "packaging" / "windows" / "check_bundle.py"

# В рабочем образе каталога `packaging/` нет: туда копируется установленный
# пакет, а не дерево репозитория, и тесты в нём гоняются по смонтированному
# `tests/`. Проверять там нечего — файл сборки в образ не попадает и попасть не
# должен. На рабочей копии и в остальных job-ах CI тесты идут как обычно.
pytestmark = pytest.mark.skipif(
    not SCRIPT.is_file(), reason="скрипт сборки доступен только в рабочей копии"
)

PYZ_NAME = "PYZ-00.pyz"


def load_script(
    monkeypatch: pytest.MonkeyPatch, *, archive: dict[str, Any], bundled: set[str]
) -> Any:
    """Загружает скрипт с подставленными читателями архивов PyInstaller."""

    class CArchiveReader:
        def __init__(self, path: str) -> None:
            self.toc = archive

        def extract(self, name: str) -> bytes:
            return b"pyz"

    class ZlibArchiveReader:
        def __init__(self, path: str) -> None:
            # Путь настоящий: скрипт пишет извлечённый PYZ во временный каталог
            # и удаляет его вместе с каталогом, а на Windows это удалось бы не
            # всегда. Пусть падает здесь, а не у того, кто соберёт релиз.
            assert Path(path).is_file()
            self.toc = bundled

    readers = types.ModuleType("PyInstaller.archive.readers")
    readers.CArchiveReader = CArchiveReader  # type: ignore[attr-defined]
    readers.ZlibArchiveReader = ZlibArchiveReader  # type: ignore[attr-defined]
    for name, module in (
        ("PyInstaller", types.ModuleType("PyInstaller")),
        ("PyInstaller.archive", types.ModuleType("PyInstaller.archive")),
        ("PyInstaller.archive.readers", readers),
    ):
        monkeypatch.setitem(sys.modules, name, module)

    spec = importlib.util.spec_from_file_location("check_bundle", SCRIPT)
    assert spec is not None and spec.loader is not None
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    # Список модулей библиотеки берётся из окружения, а `pymax` — необязательная
    # зависимость, которой в окружении тестов может не быть вовсе.
    monkeypatch.setattr(script, "installed_modules", lambda: {"pymax", "pymax.client"})
    return script


def test_complete_bundle_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    script = load_script(
        monkeypatch,
        # Вторая запись — любая не-`.pyz`, чтобы проверка выбирала нужную, а не
        # единственную. Имя без номера версии Python намеренно: иначе фикстуру
        # пришлось бы править при каждом обновлении интерпретатора, а к тому,
        # что она проверяет, версия отношения не имеет.
        archive={PYZ_NAME: None, "vcruntime140.dll": None},
        bundled={"pymax", "pymax.client", "json"},
    )
    assert script.main(["maxub.exe"]) == 0


def test_missing_module_fails(monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    """Недостача — это отказ, а не предупреждение: доложить её в exe нечем."""
    script = load_script(
        monkeypatch,
        archive={PYZ_NAME: None},
        bundled={"pymax"},
    )
    assert script.main(["maxub.exe"]) == 1
    assert "pymax.client" in capsys.readouterr().err


def test_every_exe_is_checked(monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    """В релизе два exe, и второй проверяется не меньше первого."""
    script = load_script(
        monkeypatch,
        archive={PYZ_NAME: None},
        bundled={"pymax", "pymax.client"},
    )
    assert script.main(["maxub.exe", "maxubctl.exe"]) == 0
    assert capsys.readouterr().out.count("pymax") == 2


def test_no_arguments_is_usage_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Код 2, а не 1: перепутанный вызов — это не провал проверки."""
    script = load_script(monkeypatch, archive={PYZ_NAME: None}, bundled=set())
    assert script.main([]) == 2


def test_archive_without_pyz_is_explained(monkeypatch: pytest.MonkeyPatch) -> None:
    """Иначе вместо объяснения был бы `StopIteration` без единого слова."""
    script = load_script(
        monkeypatch,
        archive={"vcruntime140.dll": None},
        bundled={"pymax", "pymax.client"},
    )
    with pytest.raises(SystemExit) as exc:
        script.main(["maxub.exe"])
    assert "PyInstaller" in str(exc.value)
