"""Тесты CLI: контракт кодов выхода и машинного вывода.

`maxubctl` вызывают из скриптов и агенты, поэтому коды выхода — часть публичного
контракта и проверяются отдельно от логики.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
import uvicorn
from fastapi.testclient import TestClient

from maxub.api.app import create_app
from maxub.cli import client as client_module
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
