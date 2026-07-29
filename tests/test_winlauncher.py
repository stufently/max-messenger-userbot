"""Обвязка автономной сборки: замок каталога, адрес демона, гонка секретов.

Тесты не требуют Windows: замок и разбор адреса написаны кроссплатформенно
именно для того, чтобы их можно было проверить здесь, а не только руками на
чужой машине. Специфичное для Windows (окно ошибки, буфер обмена, ACL) остаётся
за пределами — там проверять нечего без самой системы.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from maxub import winhost
from maxub.config import Settings

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
