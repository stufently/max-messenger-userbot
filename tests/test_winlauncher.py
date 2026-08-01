"""Обвязка автономной сборки: замок каталога, адрес демона, гонка секретов.

Тесты не требуют Windows: замок, разбор адреса и обращения к демону написаны
кроссплатформенно именно для того, чтобы их можно было проверить здесь, а не
только руками на чужой машине. Специфичное для Windows (окно ошибки, ACL)
остаётся за пределами — там проверять нечего без самой системы.
"""

from __future__ import annotations

import errno
import json
import os
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from maxub import config, winhost, winlauncher
from maxub.config import Settings
from maxub.paths import DataDirError

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


@pytest.fixture(autouse=True)
def release_instance_lock() -> object:
    """Снимает замок между тестами: он живёт в модуле до конца процесса."""
    yield
    handle = winhost._instance_lock
    if handle is not None:
        handle.close()
        winhost._instance_lock = None


# --- адрес демона -------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8765",
        "http://localhost:8765",
        "http://[::1]:8765",
        "http://127.0.0.5:1234",
    ],
)
def test_local_urls_are_accepted(url: str) -> None:
    assert winhost.is_local_daemon_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com:8765",  # чужой хост
        "https://127.0.0.1:8765",  # чужая схема
        "http://user:pass@127.0.0.1:8765",  # учётные данные
        "http://127.0.0.1:8765/path",  # путь
        "http://127.0.0.1:8765?a=1",  # запрос
        "http://8.8.8.8:8765",  # публичный адрес
        "file:///etc/passwd",
        "",
    ],
)
def test_foreign_urls_are_rejected(url: str) -> None:
    assert not winhost.is_local_daemon_url(url)


def test_runtime_with_foreign_url_is_ignored(tmp_path: Path) -> None:
    """Подменённый адрес не должен увести bearer-токен на чужой сервер.

    Проверка живости при этом даже не выполняется: до сети дело не доходит.
    """
    (tmp_path / winhost.RUNTIME_FILE).write_text(
        '{"url": "http://attacker.example", "pid": 1}', encoding="utf-8"
    )
    probed: list[str] = []

    def spy(base_url: str, health_path: str, timeout: float = 1.0) -> bool:
        probed.append(base_url)
        return True

    original = winhost.probe_health
    winhost.probe_health = spy  # type: ignore[assignment]
    try:
        assert winhost.running_instance(tmp_path, "/health") is None
    finally:
        winhost.probe_health = original  # type: ignore[assignment]
    assert probed == []


def test_runtime_roundtrip(tmp_path: Path) -> None:
    winhost.write_runtime(tmp_path, "http://127.0.0.1:9999")
    assert (tmp_path / winhost.RUNTIME_FILE).exists()
    # Чужой адрес файл не стирает: иначе завершившийся экземпляр убирал бы
    # запись живого.
    winhost.drop_runtime(tmp_path, "http://127.0.0.1:1111")
    assert (tmp_path / winhost.RUNTIME_FILE).exists()
    winhost.drop_runtime(tmp_path, "http://127.0.0.1:9999")
    assert not (tmp_path / winhost.RUNTIME_FILE).exists()


def test_broken_runtime_is_not_an_instance(tmp_path: Path) -> None:
    (tmp_path / winhost.RUNTIME_FILE).write_text("не json", encoding="utf-8")
    assert winhost.running_instance(tmp_path, "/health") is None


# --- одноразовый код входа ----------------------------------------------------

DAEMON_TOKEN = "token-iz-fajla"
HANDOFF_CODE = "odnorazovyj-kod"


class _HandoffStub(BaseHTTPRequestHandler):
    """Демон, отвечающий только на выдачу кода и только с верным токеном."""

    def do_POST(self) -> None:  # noqa: N802 — имя задано BaseHTTPRequestHandler
        if self.path != winlauncher.HANDOFF_PATH:
            self.send_error(404)
            return
        if self.headers.get("Authorization") != f"Bearer {DAEMON_TOKEN}":
            self.send_error(401)
            return
        body = json.dumps({"code": HANDOFF_CODE, "expires_in": 120}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        """Журнал сервера-заглушки в вывод тестов не нужен."""


@pytest.fixture
def handoff_server() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _HandoffStub)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_handoff_code_is_requested_with_token(handoff_server: str) -> None:
    """Токен уходит в заголовке и дальше лаунчера не идёт; в браузер — код."""
    code = winhost.request_handoff_code(handoff_server, winlauncher.HANDOFF_PATH, DAEMON_TOKEN)
    assert code == HANDOFF_CODE


def test_handoff_code_without_valid_token_is_none(handoff_server: str) -> None:
    assert winhost.request_handoff_code(handoff_server, winlauncher.HANDOFF_PATH, "ne-tot") is None
    # Недоступный демон — тоже отсутствие кода, а не исключение в потоке.
    assert winhost.request_handoff_code("http://127.0.0.1:1", "/web/handoff", DAEMON_TOKEN) is None


def test_panel_url_carries_one_time_code(handoff_server: str) -> None:
    url = winlauncher.panel_url(handoff_server, DAEMON_TOKEN)
    assert url == f"{handoff_server}{winlauncher.WEB_ENTER_PATH}?code={HANDOFF_CODE}"
    # Токен в адрес не попадает ни при каких условиях: он бы осел в истории.
    assert DAEMON_TOKEN not in url


def test_panel_url_falls_back_to_token_form(handoff_server: str) -> None:
    """Без кода панель всё равно открывается — вход по токену никуда не делся."""
    url = winlauncher.panel_url(handoff_server, "ne-tot")
    assert url == f"{handoff_server}{winlauncher.WEB_PAGE_PATH}"


def test_clipboard_helper_is_gone() -> None:
    """Токен больше не проходит через буфер обмена Windows."""
    assert not hasattr(winhost, "copy_token_to_clipboard")


# --- замок каталога данных ----------------------------------------------------


def test_second_instance_is_refused(tmp_path: Path) -> None:
    """Две копии на одной базе — это две очереди отправки на один аккаунт."""
    assert winhost.acquire_single_instance(tmp_path)
    assert not winhost.acquire_single_instance(tmp_path)


def test_lock_is_per_data_dir(tmp_path: Path) -> None:
    """Замок защищает каталог, а не процесс: чужой каталог занимать нечем."""
    first = tmp_path / "one"
    second = tmp_path / "two"
    first.mkdir()
    second.mkdir()
    assert winhost.acquire_single_instance(first)
    held = winhost._instance_lock
    assert winhost.acquire_single_instance(second)
    if held is not None:
        held.close()


def test_missing_data_dir_is_refused(tmp_path: Path) -> None:
    """Не смогли взять замок — не запускаемся: молча допустить вторую копию хуже."""
    assert not winhost.acquire_single_instance(tmp_path / "нет-такого")


# --- гонка при создании секретов ----------------------------------------------


def test_concurrent_first_start_shares_one_token(tmp_path: Path) -> None:
    """Два процесса на пустом каталоге не должны драться за файл токена.

    Раньше проигравший получал `FileExistsError` из `O_EXCL` и падал ещё до
    запуска демона. Гонка вероятностная (на старом коде воспроизводилась в
    ~95% попыток), поэтому раундов несколько: один давал бы тест, который
    изредка пропускает поломку.
    """
    for round_number in range(5):
        data_dir = tmp_path / f"round-{round_number}"
        data_dir.mkdir()
        results, errors = _resolve_token_in_parallel(data_dir, workers=4)

        assert errors == [], f"раунд {round_number}: {errors}"
        assert len(results) == 4
        assert len(set(results)) == 1, "процессы разошлись в значении токена"
        assert results[0] == (data_dir / "api_token").read_text(encoding="utf-8").strip()


def test_empty_token_file_is_awaited_not_refused(tmp_path: Path) -> None:
    """Файл появляется раньше содержимого — это окно надо переждать.

    Тот же случай, что в тесте выше, но без гонки: пустой файл здесь заполняется
    заведомо позже, чем проигравший его прочитает. Прежний код отсчитывал три
    витка без единой паузы и на этом сдавался — а на двухъядерном раннере GitHub
    победитель именно в это окно и не успевал, из-за чего прогон падал
    вероятностно, на случайной версии Python.
    """
    settings = Settings(data_dir=tmp_path, transport="stub")
    token_path = tmp_path / "api_token"
    token_path.touch(mode=0o600)

    def fill_later() -> None:
        time.sleep(0.1)
        token_path.write_text("токен-победителя", encoding="utf-8")

    writer = threading.Thread(target=fill_later)
    writer.start()
    try:
        assert settings.resolve_token() == "токен-победителя"
    finally:
        writer.join(timeout=5)


def test_secret_appears_under_its_name_already_whole(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Имя файла секрета появляется, когда значение уже записано целиком.

    Ожидание пустого файла (тест выше) — страховка, а не решение: пока имя
    занимает пустой файл, сосед может прочитать и обрезанное значение, если
    запись разошлась на части. Поэтому запись идёт во временный файл, а имя
    появляется одним `os.link`. Проверяется ровно этот порядок: на момент
    публикации целевого имени ещё нет, а в источнике — всё значение.
    """
    settings = Settings(data_dir=tmp_path, transport="stub")
    token_path = tmp_path / "api_token"
    real_link = os.link
    observed: list[tuple[bool, str]] = []

    def watching_link(src: object, dst: object) -> None:
        observed.append((token_path.exists(), Path(str(src)).read_text(encoding="utf-8")))
        real_link(str(src), str(dst))

    monkeypatch.setattr(os, "link", watching_link)
    token = settings.resolve_token()

    assert observed, "публикация прошла мимо os.link"
    taken, staged = observed[0]
    assert not taken, "имя было занято ещё до публикации"
    assert staged == token, "во временном файле лежало не всё значение"
    assert not list(tmp_path.glob("api_token.tmp-*")), "временный файл не убран"


def test_secret_survives_filesystem_without_hard_links(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """На ФС без жёстких ссылок запуск не отказывает, а пишет напрямую.

    FAT на съёмном диске и часть сетевых томов `os.link` не поддерживают. Отказ
    завести токен там был бы хуже, чем прежнее узкое окно между созданием файла
    и записью в него.
    """

    def refuse_link(src: object, dst: object) -> None:
        raise OSError(errno.EPERM, "жёсткие ссылки не поддерживаются")

    monkeypatch.setattr(os, "link", refuse_link)
    settings = Settings(data_dir=tmp_path, transport="stub")
    token = settings.resolve_token()
    token_path = tmp_path / "api_token"

    assert token == token_path.read_text(encoding="utf-8").strip()
    assert token_path.stat().st_mode & 0o777 == 0o600
    assert not list(tmp_path.glob("api_token.tmp-*")), "временный файл не убран"


def test_token_file_that_stays_empty_is_explained(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Пустой файл, который никто не заполняет, — отказ со словами, но не сразу.

    Ожидание укорочено намеренно: смысл проверки в том, что ожидание конечно, а
    не в том, сколько оно длится по умолчанию.

    Тип проверяется точно, а не как любой `RuntimeError`: на голом типе отказ
    пролетал мимо всех обработчиков — CLI печатал трассировку поверх аккуратно
    написанного текста, exe без консоли умирал молча.
    """
    monkeypatch.setattr(config, "SECRET_WAIT_SECONDS", 0.05)
    settings = Settings(data_dir=tmp_path, transport="stub")
    (tmp_path / "api_token").touch(mode=0o600)
    with pytest.raises(config.SecretError, match="пуст"):
        settings.resolve_token()
    assert issubclass(config.SecretError, DataDirError)


def _resolve_token_in_parallel(
    data_dir: Path, workers: int
) -> tuple[list[str], list[BaseException]]:
    """Запускает `resolve_token` из нескольких потоков одновременно."""
    settings = Settings(data_dir=data_dir, transport="stub")
    results: list[str] = []
    errors: list[BaseException] = []
    start = threading.Barrier(workers)

    def resolve() -> None:
        try:
            start.wait(timeout=5)
            results.append(settings.resolve_token())
        except BaseException as exc:  # noqa: BLE001 — падение потока и есть находка
            errors.append(exc)

    threads = [threading.Thread(target=resolve) for _ in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    return results, errors


# --- отказ каталога данных ----------------------------------------------------


def test_broken_data_dir_shows_a_window(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Отказ каталога — окно с текстом и код 1, а не необработанное исключение.

    Проверка появилась после ревью: `ensure_data_dir` стал бросать свой
    `DataDirError`, а лаунчер ловил только `OSError`, и для пользователя exe без
    консоли это выглядело бы как «щёлкнул — ничего не произошло». Трассировке в
    оконном процессе выводиться некуда, поэтому показать окно — единственный
    способ хоть что-то сообщить.
    """
    occupied = tmp_path / "занято"
    occupied.write_text("не каталог", encoding="utf-8")
    monkeypatch.setenv("MAXUB_DATA_DIR", str(occupied))

    shown: list[str] = []
    monkeypatch.setattr(winlauncher, "show_error", lambda message: shown.append(message))

    assert winlauncher.main() == 1
    assert len(shown) == 1
    assert "MAXUB_DATA_DIR" in shown[0]


@pytest.fixture
def launcher_probe(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[list[str], list[str]]:
    """Каталог с застрявшим пустым токеном; окна и открытые адреса — в списки."""
    monkeypatch.setattr(config, "SECRET_WAIT_SECONDS", 0.05)
    monkeypatch.setenv("MAXUB_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MAXUB_TRANSPORT", "stub")
    (tmp_path / "api_token").touch(mode=0o600)

    shown: list[str] = []
    opened: list[str] = []
    monkeypatch.setattr(winlauncher, "show_error", lambda message: shown.append(message))
    monkeypatch.setattr(winlauncher.webbrowser, "open", lambda url: opened.append(url))
    # Демон в тесте поднимать нечем и незачем: до него дойти не должны, а если
    # дойдут — пусть это будет видно как ошибка, а не как повисший тест.
    monkeypatch.setattr(
        winlauncher, "serve", lambda settings: pytest.fail("демон не должен запускаться")
    )
    return shown, opened


def test_stuck_token_shows_a_window_on_first_start(
    launcher_probe: tuple[list[str], list[str]], tmp_path: Path
) -> None:
    """Первый запуск с застрявшим файлом токена: окно, код 1, запись в журнал.

    Раньше здесь ловился только `OSError`, и отказ секрета улетал наружу мимо
    окна: у exe без консоли это выглядит как «щёлкнул — ничего не произошло»,
    причём и в `launcher.log` не оставалось ни строки.

    Журнал проверяется файлом, а не `caplog`: лаунчер настраивает логирование
    сам с `force=True`, снимая чужие обработчики, — и файл как раз то место,
    куда пользователя отправляет окно с ошибкой.
    """
    shown, opened = launcher_probe
    assert winlauncher.main() == 1
    assert len(shown) == 1
    assert "токен" in shown[0].lower()
    assert not opened, "браузер открывать нечем: токена нет"

    journal = (tmp_path / "launcher.log").read_text(encoding="utf-8")
    assert "не удалось подготовить токен доступа" in journal
    assert "SecretError" in journal, "в журнале нужна трассировка, а не только строка"


def test_stuck_token_shows_a_window_when_daemon_already_runs(
    launcher_probe: tuple[list[str], list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Та же ветка, но демон уже работает: окно вместо падения по дороге к панели.

    Ветвь отдельная, потому что и падала она отдельно: там `resolve_token`
    стоял вообще без обработчика.
    """
    shown, opened = launcher_probe
    monkeypatch.setattr(
        winlauncher, "running_instance", lambda data_dir, path: "http://127.0.0.1:8765"
    )

    assert winlauncher.main() == 1
    assert len(shown) == 1
    assert "токен" in shown[0].lower()
    assert not opened, "панель без токена открывать некуда"
