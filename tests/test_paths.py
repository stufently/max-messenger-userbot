"""Выбор каталога данных: платформы, переменные окружения и обе модели настроек.

Проверяется не столько сама склейка путей, сколько два свойства, на которых
держится запуск без Docker. Первое: демон и клиент обязаны смотреть в один
каталог — разойдись они, демон создаст токен в одном месте, а `maxub` будет
искать его в другом и молча получит «нет доступа». Второе: заданный человеком
`MAXUB_DATA_DIR` главнее любого вычисленного пути.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from maxub.cli.main import _run
from maxub.config import ClientSettings, Settings
from maxub.paths import DataDirError, default_data_dir


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Убирает переменные, которые могли прийти из окружения прогона.

    В рабочем образе задан `MAXUB_DATA_DIR=/data`, и без уборки половина
    проверок ниже сравнивала бы дефолт с ним, а не с вычисленным путём.
    """
    for name in ("MAXUB_DATA_DIR", "XDG_DATA_HOME", "LOCALAPPDATA"):
        monkeypatch.delenv(name, raising=False)


def test_linux_uses_xdg_data_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "share"))
    assert default_data_dir("linux") == tmp_path / "share" / "maxub"


def test_linux_without_xdg_falls_back_to_local_share(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("/home/tester")))
    assert default_data_dir("linux") == Path("/home/tester/.local/share/maxub")


def test_relative_xdg_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    """Спецификация XDG требует игнорировать относительный путь.

    Иначе данные оседали бы там, откуда запустили демон, и при следующем запуске
    из другого каталога он не нашёл бы ни токена, ни базы с сессиями.
    """
    monkeypatch.setenv("XDG_DATA_HOME", "relative/share")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("/home/tester")))
    assert default_data_dir("linux") == Path("/home/tester/.local/share/maxub")


def test_empty_xdg_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    """Пустая переменная — это «не задано», а не корень файловой системы."""
    monkeypatch.setenv("XDG_DATA_HOME", "")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("/home/tester")))
    assert default_data_dir("linux") == Path("/home/tester/.local/share/maxub")


def test_windows_uses_localappdata(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    assert default_data_dir("win32") == tmp_path / "Local" / "maxub"


def test_windows_without_localappdata_uses_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    """Переменной нет в служебных сеансах и под Wine — путь собирается сам."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("/users/tester")))
    assert default_data_dir("win32") == Path("/users/tester/AppData/Local/maxub")


def test_macos_uses_application_support(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("/Users/tester")))
    assert default_data_dir("darwin") == Path("/Users/tester/Library/Application Support/maxub")


def test_xdg_does_not_leak_into_windows(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Ветки не должны перепутаться: под Windows XDG ничего не решает."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "share"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    assert default_data_dir("win32") == tmp_path / "Local" / "maxub"


def test_daemon_and_client_agree_on_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Главная проверка: демон и клиент берут один и тот же каталог."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "share"))
    assert Settings().data_dir == ClientSettings().data_dir == default_data_dir()


def test_env_wins_over_computed_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MAXUB_DATA_DIR", str(tmp_path / "chosen"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "share"))
    assert Settings().data_dir == tmp_path / "chosen"
    assert ClientSettings().data_dir == tmp_path / "chosen"


def test_default_is_read_at_creation_not_at_import(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Дефолт вычисляется на каждый экземпляр.

    Будь значение снято один раз при импорте, смена `HOME` или `XDG_DATA_HOME`
    в уже запущенном процессе не имела бы силы — а на этом стоят и тесты, и
    запуск демона из-под другого пользователя.
    """
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "first"))
    first = Settings().data_dir
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "second"))
    assert Settings().data_dir != first
    assert Settings().data_dir == tmp_path / "second" / "maxub"


def test_client_finds_token_written_by_daemon(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Сквозная проверка: демон создал токен, клиент нашёл его без настроек."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "share"))
    token = Settings().resolve_token()
    assert ClientSettings().resolve_token() == token


def test_missing_permissions_are_explained(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Отказ доступа объясняется словами и называет переменную для обхода."""
    # `geteuid` нет в Windows, а биты доступа там ничего не решают: проверять
    # отказ негде. Под root его тоже не устроить — он пишет куда угодно.
    if not hasattr(os, "geteuid") or os.geteuid() == 0:
        pytest.skip("отказ доступа так не устроить: Windows или root")
    blocked = tmp_path / "blocked"
    blocked.mkdir(mode=0o500)
    monkeypatch.setenv("MAXUB_DATA_DIR", str(blocked / "data"))
    with pytest.raises(RuntimeError, match="MAXUB_DATA_DIR"):
        Settings().ensure_data_dir()


def test_file_in_place_of_directory_is_explained(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Иначе вместо объяснения был бы `FileExistsError` без единого слова."""
    occupied = tmp_path / "occupied"
    occupied.write_text("не каталог", encoding="utf-8")
    monkeypatch.setenv("MAXUB_DATA_DIR", str(occupied))
    with pytest.raises(DataDirError, match="не каталог"):
        Settings().ensure_data_dir()


def test_cli_explains_broken_data_dir_instead_of_traceback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Отказ каталога — сообщение и код 1, а не трассировка.

    Ловится именно `DataDirError`, а не любой `RuntimeError`: иначе CLI глотал бы
    и чужие ошибки, выдавая их за проблему с каталогом.
    """
    occupied = tmp_path / "occupied"
    occupied.write_text("не каталог", encoding="utf-8")
    monkeypatch.setenv("MAXUB_DATA_DIR", str(occupied))
    with pytest.raises(SystemExit) as exc:
        _run(["token"])
    assert exc.value.code == 1
    # Сравнивается имя переменной, а не фраза: Rich переносит строки по ширине
    # терминала и рвёт текст в непредсказуемом месте — «не каталог» в выводе
    # оказывается «не\nкаталог». Проверять надо то, ради чего сообщение и
    # написано: что человеку названа переменная, которой отказ лечится.
    printed = capsys.readouterr().err
    assert "MAXUB_DATA_DIR" in printed
    assert "Traceback" not in printed
