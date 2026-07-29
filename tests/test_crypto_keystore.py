"""Ключ шифрования: выбор источника, деградация и миграция.

Живого DPAPI и Secret Service здесь нет и быть не может — тесты идут в
Linux-контейнере без сеансового демона. Поэтому платформенные вызовы
подменяются через протокол [KeyStore][maxub.core.keystore.KeyStore], а
проверяется то, что от них не зависит: порядок источников, поведение при отказе
хранилища и судьба существующего `secret.key`.
"""

from __future__ import annotations

import types
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from maxub import config
from maxub.config import Settings
from maxub.core import keystore, keystore_backends
from maxub.core.crypto import SecretBox, generate_key
from maxub.core.keystore import KeyStoreError, SecretKeyConflict
from maxub.core.keystore_backends import DpapiKeyStore, SecretServiceKeyStore


class FakeStore:
    """Хранилище ОС в памяти: то же поведение без платформы под ним."""

    name = "fake"

    def __init__(self, key: str | None = None, fails: str = "") -> None:
        self.key = key
        self.fails = set(fails.split())
        self.saved: list[str] = []

    def load(self) -> str | None:
        if "load" in self.fails:
            raise KeyStoreError("связка ключей заблокирована")
        return self.key

    def save(self, key: str) -> None:
        if "save" in self.fails:
            raise KeyStoreError("связка ключей заблокирована")
        self.saved.append(key)
        self.key = key


#: Подменяет платформенное хранилище заданным (или его отсутствием).
InstallStore = Callable[[FakeStore | None], None]


@pytest.fixture
def install_store(monkeypatch: pytest.MonkeyPatch) -> InstallStore:
    """Подменять надо имя в `config`, а не в модуле хранилищ.

    `config` берёт `open_keystore` при импорте, поэтому подмена в
    `keystore_backends` до него уже не доедет — там останется прежняя ссылка.
    """

    def install(store: FakeStore | None) -> None:
        monkeypatch.setattr(
            config, "open_keystore", lambda data_dir, platform=None: store, raising=True
        )

    return install


def make_settings(tmp_path: Path, **kwargs: Any) -> Settings:
    return Settings(data_dir=tmp_path, transport="stub", **kwargs)


def write_key_file(settings: Settings, key: str) -> None:
    settings.secret_key_path.write_text(key, encoding="utf-8")


# --- порядок источников -------------------------------------------------------


def test_env_key_wins_over_store(tmp_path: Path, install_store: InstallStore) -> None:
    """Явно заданный ключ старше хранилища: к хранилищу даже не обращаемся."""
    store = FakeStore(key=generate_key())
    install_store(store)
    env_key = generate_key()

    settings = make_settings(tmp_path, secret_key=env_key)

    assert settings.resolve_secret_key() == env_key
    assert store.saved == []
    assert not settings.secret_key_path.exists()


def test_store_wins_over_file(tmp_path: Path, install_store: InstallStore) -> None:
    """Ключ есть в хранилище — файл рядом с базой не заводится."""
    stored = generate_key()
    install_store(FakeStore(key=stored))
    settings = make_settings(tmp_path)

    assert settings.resolve_secret_key() == stored
    assert not settings.secret_key_path.exists()


def test_file_used_when_no_store(tmp_path: Path, install_store: InstallStore) -> None:
    """Хранилища ОС нет — это не ошибка, работаем файлом, как раньше."""
    install_store(None)
    settings = make_settings(tmp_path)

    key = settings.resolve_secret_key()

    assert settings.secret_key_path.exists()
    assert settings.secret_key_path.stat().st_mode & 0o777 == 0o600
    assert make_settings(tmp_path).resolve_secret_key() == key


def test_store_mode_file_ignores_store(tmp_path: Path, install_store: InstallStore) -> None:
    """`file` отключает хранилище, даже когда оно доступно."""
    store = FakeStore(key=generate_key())
    install_store(store)
    settings = make_settings(tmp_path, secret_key_store="file")

    key = settings.resolve_secret_key()

    assert key != store.key
    assert settings.secret_key_path.exists()


def test_store_mode_os_requires_store(tmp_path: Path, install_store: InstallStore) -> None:
    """`os` — явное требование привязки: без хранилища демон не стартует."""
    install_store(None)
    settings = make_settings(tmp_path, secret_key_store="os")

    with pytest.raises(RuntimeError, match="MAXUB_SECRET_KEY_STORE=os"):
        settings.resolve_secret_key()


# --- отказ хранилища ----------------------------------------------------------


def test_store_failure_falls_back_to_file(tmp_path: Path, install_store: InstallStore) -> None:
    """Хранилище отвечает ошибкой — берём ключ из файла, а не падаем."""
    install_store(FakeStore(key=generate_key(), fails="load"))
    settings = make_settings(tmp_path)
    file_key = generate_key()
    write_key_file(settings, file_key)

    assert settings.resolve_secret_key() == file_key


def test_store_failure_on_first_run_creates_file(
    tmp_path: Path, install_store: InstallStore
) -> None:
    """Ни хранилища, ни файла, ни базы — терять нечего, заводим ключ в файле."""
    install_store(FakeStore(fails="save"))
    settings = make_settings(tmp_path)

    key = settings.resolve_secret_key()

    assert settings.secret_key_path.read_text(encoding="utf-8").strip() == key


def test_store_failure_over_existing_db_refuses(
    tmp_path: Path, install_store: InstallStore
) -> None:
    """База есть, а ключ недоступен — новый не создаём: это потеря сессий."""
    install_store(FakeStore(key=generate_key(), fails="load"))
    settings = make_settings(tmp_path)
    settings.ensure_data_dir()
    settings.db_path.touch()

    with pytest.raises(RuntimeError, match="нечитаемыми"):
        settings.resolve_secret_key()


def test_store_mode_os_does_not_fall_back(tmp_path: Path, install_store: InstallStore) -> None:
    """В режиме `os` отказ хранилища не подменяется файлом."""
    install_store(FakeStore(key=generate_key(), fails="load"))
    settings = make_settings(tmp_path, secret_key_store="os")
    write_key_file(settings, generate_key())

    with pytest.raises(RuntimeError, match="отказом"):
        settings.resolve_secret_key()


def test_conflicting_keys_refuse_to_start(tmp_path: Path, install_store: InstallStore) -> None:
    """Разные ключи в хранилище и в файле — выбирать за человека нельзя."""
    install_store(FakeStore(key=generate_key()))
    settings = make_settings(tmp_path)
    write_key_file(settings, generate_key())

    with pytest.raises(SecretKeyConflict):
        settings.resolve_secret_key()


# --- пригодность ключа --------------------------------------------------------


def test_key_from_store_opens_sealed_secret(tmp_path: Path, install_store: InstallStore) -> None:
    """Ключ из хранилища действительно расшифровывает то, что им зашифровано."""
    install_store(FakeStore(key=generate_key()))
    settings = make_settings(tmp_path)
    payload = {"session": "секрет"}

    envelope = SecretBox(settings.resolve_secret_key()).seal(payload)

    # Второй запуск демона: ключ снова берётся из хранилища.
    assert SecretBox(make_settings(tmp_path).resolve_secret_key()).open(envelope) == payload


def test_generated_key_is_stored_and_reused(tmp_path: Path, install_store: InstallStore) -> None:
    """Пустое хранилище на первом запуске заполняется и переживает перезапуск."""
    store = FakeStore()
    install_store(store)
    settings = make_settings(tmp_path)

    key = settings.resolve_secret_key()

    assert store.saved == [key]
    assert make_settings(tmp_path).resolve_secret_key() == key
    assert not settings.secret_key_path.exists()


# --- миграция существующей установки ------------------------------------------


def test_migration_copies_key_and_keeps_file(tmp_path: Path, install_store: InstallStore) -> None:
    """Ключ копируется в хранилище, файл остаётся: откат должен быть возможен."""
    store = FakeStore()
    install_store(store)
    settings = make_settings(tmp_path)
    file_key = generate_key()
    write_key_file(settings, file_key)

    assert settings.resolve_secret_key() == file_key
    assert store.key == file_key
    assert settings.secret_key_path.read_text(encoding="utf-8").strip() == file_key


def test_migration_is_reversible(tmp_path: Path, install_store: InstallStore) -> None:
    """После миграции возврат к файлу ничего не ломает: ключ тот же."""
    install_store(FakeStore())
    file_key = generate_key()
    write_key_file(make_settings(tmp_path), file_key)
    make_settings(tmp_path).resolve_secret_key()

    install_store(None)
    assert make_settings(tmp_path).resolve_secret_key() == file_key


def test_drop_file_removes_it_only_when_asked(tmp_path: Path, install_store: InstallStore) -> None:
    """Файл исчезает только по явному указанию — и ключ остаётся доступен."""
    store = FakeStore()
    install_store(store)
    settings = make_settings(tmp_path, secret_key_drop_file=True)
    file_key = generate_key()
    write_key_file(settings, file_key)

    assert settings.resolve_secret_key() == file_key
    assert not settings.secret_key_path.exists()
    assert store.key == file_key
    assert make_settings(tmp_path).resolve_secret_key() == file_key


def test_drop_file_leaves_trace_without_key(tmp_path: Path, install_store: InstallStore) -> None:
    """На месте удалённого файла остаётся записка — но без самого ключа."""
    install_store(FakeStore())
    settings = make_settings(tmp_path, secret_key_drop_file=True)
    file_key = generate_key()
    write_key_file(settings, file_key)

    settings.resolve_secret_key()

    note = keystore.moved_marker(settings.secret_key_path).read_text(encoding="utf-8")
    assert file_key not in note
    assert "MAXUB_SECRET_KEY_DROP_FILE" in note


def test_trace_blocks_new_key_when_store_disappears(
    tmp_path: Path, install_store: InstallStore
) -> None:
    """Хранилище пропало совсем — новый ключ поверх переноса не создаём.

    Именно так выглядит удалённый `keyring` или чужой профиль: `open_keystore`
    возвращает `None`, и без следа переноса это неотличимо от первого запуска.
    """
    install_store(FakeStore())
    settings = make_settings(tmp_path, secret_key_drop_file=True)
    write_key_file(settings, generate_key())
    settings.resolve_secret_key()

    install_store(None)
    with pytest.raises(RuntimeError, match="перенесён в хранилище"):
        make_settings(tmp_path).resolve_secret_key()


def test_restored_file_works_despite_trace(tmp_path: Path, install_store: InstallStore) -> None:
    """Ключ вернули в файл вручную — след переноса больше не мешает."""
    install_store(FakeStore())
    settings = make_settings(tmp_path, secret_key_drop_file=True)
    file_key = generate_key()
    write_key_file(settings, file_key)
    settings.resolve_secret_key()

    install_store(None)
    write_key_file(settings, file_key)

    assert make_settings(tmp_path).resolve_secret_key() == file_key


def test_drop_file_skipped_when_store_broken(tmp_path: Path, install_store: InstallStore) -> None:
    """Хранилище не приняло ключ — файл не трогаем, иначе потеряем его вовсе."""
    install_store(FakeStore(fails="save"))
    settings = make_settings(tmp_path, secret_key_drop_file=True)
    file_key = generate_key()
    write_key_file(settings, file_key)

    assert settings.resolve_secret_key() == file_key
    assert settings.secret_key_path.exists()


def test_store_racing_write_wins(tmp_path: Path) -> None:
    """Ключ, записанный соседним процессом, важнее только что своего.

    Иначе два демона на одном каталоге разойдутся ключами, и сессии одного из
    них станут нечитаемыми.
    """

    class RacingStore(FakeStore):
        def save(self, key: str) -> None:
            super().save(key)
            self.key = "чужой-ключ"

    assert keystore.resolve_key(RacingStore(), tmp_path / "secret.key") == "чужой-ключ"


# --- выбор реализации под платформу -------------------------------------------


def test_open_keystore_picks_dpapi_on_windows(tmp_path: Path) -> None:
    assert isinstance(keystore_backends.open_keystore(tmp_path, platform="win32"), DpapiKeyStore)


def test_dpapi_outside_windows_degrades(tmp_path: Path) -> None:
    """DPAPI вне Windows даёт KeyStoreError, а не падает чем попало.

    Так `resolve_key` отступит к файлу, а не уронит демон.
    """
    store = DpapiKeyStore(tmp_path / "secret.key.dpapi")

    with pytest.raises(KeyStoreError):
        store.save(generate_key())
    assert keystore.resolve_key(store, tmp_path / "secret.key") is None


def test_blob_written_whole_and_closed(tmp_path: Path) -> None:
    """Конверт DPAPI пишется целиком, с правами 0600 и поверх прежнего.

    Сам DPAPI отсюда не проверить, а запись на диск — вполне: половина конверта
    означала бы потерянный ключ.
    """
    path = tmp_path / "secret.key.dpapi"
    keystore.write_blob(path, b"\x00\x01" * 32)
    keystore.write_blob(path, b"\x02" * 1024)

    assert path.read_bytes() == b"\x02" * 1024
    assert path.stat().st_mode & 0o777 == 0o600
    assert not path.with_name(path.name + ".tmp").exists()


def test_missing_keyring_means_no_store(tmp_path: Path) -> None:
    """Необязательной зависимости нет — хранилища нет, и это не ошибка."""
    assert keystore_backends.open_keystore(tmp_path, platform="linux") is None


def install_backend(monkeypatch: pytest.MonkeyPatch, origin: str) -> None:
    """Подсовывает `keyring` с бэкендом из заданного модуля."""
    backend = type("Keyring", (), {})
    backend.__module__ = origin
    fake = types.SimpleNamespace(get_keyring=lambda: backend())
    monkeypatch.setattr(keystore_backends.importlib, "import_module", lambda name: fake)


def test_keyring_without_backend_means_no_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Библиотека есть, но рабочего бэкенда нет — заглушку за хранилище не считаем."""
    install_backend(monkeypatch, "keyring.backends.fail")

    assert keystore_backends.open_keystore(tmp_path, platform="linux") is None


def test_file_backend_is_not_a_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Файловый бэкенд keyring — это снова файл, а не хранилище ОС."""
    install_backend(monkeypatch, "keyrings.alt.file")

    assert keystore_backends.open_keystore(tmp_path, platform="linux") is None


def test_secret_service_backend_is_a_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Настоящий Secret Service принимается."""
    install_backend(monkeypatch, "keyring.backends.SecretService")

    store = keystore_backends.open_keystore(tmp_path, platform="linux")
    assert isinstance(store, SecretServiceKeyStore)


def test_secret_service_errors_become_keystore_error() -> None:
    """Ошибки бэкенда keyring не летят наружу как есть."""

    def boom(*args: str) -> str:
        raise OSError("d-bus недоступен")

    store = SecretServiceKeyStore("/data", types.SimpleNamespace(get_password=boom))

    with pytest.raises(KeyStoreError, match="Secret Service"):
        store.load()


def test_empty_store_over_existing_db_refuses(tmp_path: Path, install_store: InstallStore) -> None:
    """Пустое хранилище при живой базе — потерянный ключ, а не первый запуск.

    Раньше проверка стояла только на пути отказа хранилища: доступное, но пустое
    хранилище молча получало новый ключ, и все сохранённые сессии становились
    нечитаемыми.
    """
    store = FakeStore()
    install_store(store)
    settings = make_settings(tmp_path)
    settings.ensure_data_dir()
    settings.db_path.touch()

    with pytest.raises(keystore.SecretKeyMissing):
        settings.resolve_secret_key()
    assert store.saved == []


def test_empty_store_on_first_run_creates_key(tmp_path: Path, install_store: InstallStore) -> None:
    """Без базы терять нечего — обычный первый запуск не должен ломаться."""
    store = FakeStore()
    install_store(store)

    key = make_settings(tmp_path).resolve_secret_key()

    assert store.saved == [key]
