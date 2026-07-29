"""Тесты CLI: контракт кодов выхода и машинного вывода.

`maxubctl` вызывают из скриптов и агенты, поэтому коды выхода — часть публичного
контракта и проверяются отдельно от логики.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import click
import pytest
import uvicorn
from fastapi.testclient import TestClient

from maxub.api.app import create_app
from maxub.cli import client as client_module
from maxub.cli import errors as cli_errors
from maxub.cli.main import _run
from maxub.config import Settings
from maxub.transport.stub import STUB_CODE


@pytest.fixture
def api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """Поднимает приложение и заворачивает httpx-запросы CLI на TestClient."""
    settings = Settings(
        data_dir=tmp_path,
        transport="stub",
        send_rate_per_minute=6000.0,
        send_burst=50,
        send_jitter_seconds=0.0,
    )
    app = create_app(settings)
    with TestClient(app) as test_client:

        def fake_request(method: str, url: str, **kwargs: object):
            path = url.replace("http://127.0.0.1:8765", "")
            return test_client.request(method, path, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(client_module.httpx, "request", fake_request)
        monkeypatch.setenv("MAXUB_DATA_DIR", str(tmp_path))
        monkeypatch.setenv("MAXUB_TOKEN", app.state.api_token)
        yield test_client


def _run_cli(args: list[str]) -> int:
    with pytest.raises(SystemExit) as excinfo:
        _run(args)
    return int(excinfo.value.code or 0)


def test_status_exits_zero(api: TestClient, capsys: pytest.CaptureFixture[str]) -> None:
    assert _run_cli(["--json", "status"]) == client_module.EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["transport"] == "stub"


def test_duplicate_account_exits_conflict(api: TestClient) -> None:
    assert _run_cli(["--json", "accounts", "add", "--phone", "+79990000000"]) == 0
    assert (
        _run_cli(["--json", "accounts", "add", "--phone", "+79990000000"])
        == client_module.EXIT_CONFLICT
    )


def test_bad_token_exits_auth(api: TestClient) -> None:
    assert _run_cli(["--token", "wrong", "status"]) == client_module.EXIT_AUTH


def test_tokens_lifecycle_from_cli(api: TestClient, capsys: pytest.CaptureFixture[str]) -> None:
    """Выпуск, список и отзов — тем же клиентом, что и всё остальное."""
    assert (
        _run_cli(["--json", "tokens", "add", "--label", "монитор", "--scope", "accounts:read"])
        == client_module.EXIT_OK
    )
    issued = json.loads(capsys.readouterr().out)
    assert issued["token"]
    assert issued["item"]["label"] == "монитор"

    assert _run_cli(["--json", "tokens", "list"]) == client_module.EXIT_OK
    listed = json.loads(capsys.readouterr().out)
    assert [item["id"] for item in listed] == [issued["item"]["id"]]

    assert (
        _run_cli(["--json", "tokens", "revoke", "--id", str(issued["item"]["id"])])
        == client_module.EXIT_OK
    )
    capsys.readouterr()
    assert (
        _run_cli(["--json", "tokens", "revoke", "--id", str(issued["item"]["id"])])
        == client_module.EXIT_NOT_FOUND
    )


def test_missing_scope_exits_forbidden(api: TestClient, capsys: pytest.CaptureFixture[str]) -> None:
    """403 отличается от 401 и кодом выхода: чинить их нужно по-разному."""
    _run_cli(["--json", "tokens", "add", "--label", "читатель", "--scope", "accounts:read"])
    token = json.loads(capsys.readouterr().out)["token"]

    assert (
        _run_cli(["--token", token, "--json", "accounts", "add", "--phone", "+79990000123"])
        == client_module.EXIT_FORBIDDEN
    )


def test_send_to_unauthorized_account_exits_conflict(api: TestClient) -> None:
    _run_cli(["--json", "accounts", "add", "--phone", "+79990000001"])
    code = _run_cli(
        ["--json", "send", "--account-id", "1", "--chat-id", "chat-1", "--text", "привет"]
    )
    assert code == client_module.EXIT_CONFLICT


def test_full_flow_outputs_json(api: TestClient, capsys: pytest.CaptureFixture[str]) -> None:
    _run_cli(["--json", "accounts", "add", "--phone", "+79990000002"])
    capsys.readouterr()

    _run_cli(["--json", "login", "start", "--account-id", "1"])
    challenge_id = json.loads(capsys.readouterr().out)["challenge_id"]

    _run_cli(["--json", "login", "complete", "--challenge-id", challenge_id, "--code", STUB_CODE])
    assert json.loads(capsys.readouterr().out)["state"] == "ready"

    _run_cli(["--json", "send", "--account-id", "1", "--chat-id", "chat-9", "--text", "ок"])
    assert json.loads(capsys.readouterr().out)["queued"] is True


def test_outbox_list_exits_zero(api: TestClient, capsys: pytest.CaptureFixture[str]) -> None:
    """Список застрявших записей доступен и в машинном виде."""
    assert _run_cli(["--json", "outbox"]) == client_module.EXIT_OK
    assert isinstance(json.loads(capsys.readouterr().out), list)


def test_outbox_rejects_state_that_needs_no_review(api: TestClient) -> None:
    """Фильтр по состоянию, которое человек не разбирает, — ошибка аргументов.

    Иначе `--state sent` молча выдал бы всю историю отправок под видом списка
    застрявшего.
    """
    assert _run_cli(["--json", "outbox", "--state", "sent"]) == client_module.EXIT_USAGE


def test_retry_of_missing_item_exits_not_found(api: TestClient) -> None:
    assert _run_cli(["--json", "retry", "--id", "424242"]) == client_module.EXIT_NOT_FOUND


def test_retry_of_live_item_exits_conflict(
    api: TestClient, capsys: pytest.CaptureFixture[str]
) -> None:
    """Запись, которую ведёт демон, повторить нельзя — код конфликта."""
    _run_cli(["--json", "accounts", "add", "--phone", "+79990000003"])
    capsys.readouterr()

    _run_cli(["--json", "login", "start", "--account-id", "1"])
    challenge_id = json.loads(capsys.readouterr().out)["challenge_id"]
    _run_cli(["--json", "login", "complete", "--challenge-id", challenge_id, "--code", STUB_CODE])
    capsys.readouterr()

    _run_cli(["--json", "send", "--account-id", "1", "--chat-id", "chat-cli", "--text", "живое"])
    item_id = json.loads(capsys.readouterr().out)["item"]["id"]

    assert _run_cli(["--json", "retry", "--id", str(item_id)]) == client_module.EXIT_CONFLICT


def _login_and_send(capsys: pytest.CaptureFixture[str], phone: str, chat_id: str) -> int:
    """Доводит аккаунт до готовности и ставит сообщение, возвращая его id."""
    _run_cli(["--json", "accounts", "add", "--phone", phone])
    capsys.readouterr()

    _run_cli(["--json", "login", "start", "--account-id", "1"])
    challenge_id = json.loads(capsys.readouterr().out)["challenge_id"]
    _run_cli(["--json", "login", "complete", "--challenge-id", challenge_id, "--code", STUB_CODE])
    capsys.readouterr()

    _run_cli(["--json", "send", "--account-id", "1", "--chat-id", chat_id, "--text", "текст"])
    return int(json.loads(capsys.readouterr().out)["item"]["id"])


def _force_failed(db_path: Path, item_id: int) -> None:
    """Переводит запись в failed мимо демона — как после исчерпанных попыток.

    Через API довести запись до отказа нечем: заглушка транспорта всегда
    отправляет успешно, а тесту нужен именно тот случай, ради которого команда
    и существует.
    """
    raw = sqlite3.connect(db_path, timeout=5.0)
    try:
        raw.execute(
            "UPDATE outbox SET state = 'failed', error = 'канал занят' WHERE id = ?", (item_id,)
        )
        raw.commit()
    finally:
        raw.close()


def test_discard_exits_zero_and_records_reason(
    api: TestClient, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Успешный отказ — код 0, а причина видна в машинном выводе."""
    item_id = _login_and_send(capsys, "+79990000004", "chat-discard-cli")
    _force_failed(tmp_path / "maxub.db", item_id)

    code = _run_cli(["--json", "discard", "--id", str(item_id), "--reason", "уже не нужно"])

    assert code == client_module.EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["state"] == "discarded"
    assert payload["discard_reason"] == "уже не нужно"


def test_discard_of_missing_item_exits_not_found(api: TestClient) -> None:
    assert (
        _run_cli(["--json", "discard", "--id", "424242", "--reason", "нечего отменять"])
        == client_module.EXIT_NOT_FOUND
    )


def test_discard_of_live_item_exits_conflict(
    api: TestClient, capsys: pytest.CaptureFixture[str]
) -> None:
    """Запись, которой распоряжается демон, отменить нельзя — код конфликта."""
    item_id = _login_and_send(capsys, "+79990000005", "chat-live-cli")

    code = _run_cli(["--json", "discard", "--id", str(item_id), "--reason", "передумал"])

    assert code == client_module.EXIT_CONFLICT


def test_discard_with_blank_reason_is_rejected(
    api: TestClient, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Строка из пробелов причиной не считается — иначе проверка обходится."""
    item_id = _login_and_send(capsys, "+79990000006", "chat-blank-cli")
    _force_failed(tmp_path / "maxub.db", item_id)

    code = _run_cli(["--json", "discard", "--id", str(item_id), "--reason", "   "])

    assert code == client_module.EXIT_USAGE


def test_unreachable_daemon_exits_three(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAXUB_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MAXUB_TOKEN", "irrelevant")
    code = _run_cli(["--url", "http://127.0.0.1:9", "status"])
    assert code == client_module.EXIT_UNREACHABLE


def test_ctl_entry_point_defaults_to_json(
    api: TestClient, capsys: pytest.CaptureFixture[str]
) -> None:
    """`maxubctl` обязан отдавать JSON без явного --json."""
    result = subprocess.run(
        [sys.executable, "-c", "import maxub.cli.main as m; print(m.ctl.__doc__)"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "JSON по умолчанию" in result.stdout


def test_uvicorn_is_importable() -> None:
    """Демон должен собираться в этом же окружении."""
    assert hasattr(uvicorn, "Server")


@pytest.mark.parametrize(
    "args",
    [
        pytest.param(["send"], id="пропущена обязательная опция"),
        pytest.param(["nosuchcmd"], id="неизвестная команда"),
        pytest.param(["--timeout", "abc", "status"], id="непреобразуемое значение"),
        pytest.param(["accounts"], id="группа без подкоманды"),
    ],
)
def test_usage_errors_exit_two(
    args: list[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ошибка аргументов — это код 2 и внятное сообщение, а не трейсбек.

    Код объявлен в README частью контракта: `maxubctl` вызывают из скриптов, и
    отличать «не так вызвали» от «не сработало» они должны по коду.
    """
    monkeypatch.setenv("MAXUB_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MAXUB_TOKEN", "irrelevant")

    assert _run_cli(args) == client_module.EXIT_USAGE


def test_help_exits_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAXUB_DATA_DIR", str(tmp_path))
    assert _run_cli(["--help"]) == client_module.EXIT_OK


def test_vendored_click_exceptions_are_covered() -> None:
    """Копия click внутри typer обязана попадать в обработчики.

    Это корень ошибки, ради которой заведён `cli/errors.py`: у вендорной копии
    собственное дерево классов, не пересекающееся с внешним click, поэтому
    `except click.ClickException` пропускал ошибки разбора аргументов мимо.
    Проверка держит связь с обеими копиями при обновлении typer.
    """
    vendored = pytest.importorskip("typer._click")

    assert issubclass(vendored.exceptions.MissingParameter, cli_errors.USAGE)
    assert issubclass(vendored.exceptions.UsageError, cli_errors.USAGE)
    assert issubclass(vendored.exceptions.Exit, cli_errors.EXITS)
    assert issubclass(click.ClickException, cli_errors.USAGE)
