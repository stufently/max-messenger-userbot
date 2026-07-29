"""Сборка клиента PyMax: загрузка библиотеки, настройки, выдача сессии.

Отделено от самого адаптера по границе ответственности: здесь всё, что знает
про устройство PyMax — как его импортировать, чем настраивать и что именно
сохранять между запусками. В адаптере остаётся контракт транспорта.
"""

from __future__ import annotations

from typing import Any

from maxub.core.models import Session
from maxub.transport.base import TransportAuthError, TransportPermanent
from maxub.transport.pymax_runtime import ClientRuntime
from maxub.transport.pymax_session import Envelope, encode


def load_pymax() -> Any:
    """Импортирует PyMax по требованию.

    Импорт отложен намеренно: без библиотеки платформа обязана работать как
    раньше на транспорте `stub`, а выбор `pymax` — объяснять, чего не хватает,
    а не падать `ImportError` при старте демона.
    """
    try:
        import pymax
    except ImportError as exc:
        raise TransportPermanent(
            "транспорт pymax требует библиотеку PyMax: "
            "установите её как pip install 'max-userbot[pymax]' (пакет maxapi-python)"
        ) from exc
    return pymax


def build_extra_config(
    pymax: Any,
    runtime: ClientRuntime,
    *,
    web: bool,
    proxy: str | None,
    request_timeout: float,
    envelope: Envelope | None = None,
    device_id: str | None = None,
) -> Any:
    """Собирает `ExtraConfig` и запоминает его в runtime."""
    extra = pymax.ExtraConfig(
        token=envelope.token if envelope else None,
        device_id=device_id,
        proxy=proxy,
        request_timeout=request_timeout,
        # Переподключением владеет надзор ядра: два механизма повторов на одном
        # соединении спорили бы за задержки, а ядро вдобавок не узнало бы об
        # обрыве и держало аккаунт «готовым» без соединения.
        reconnect=False,
        # Телеметрию MAX адаптер не шлёт: выдумывать за пользователя
        # навигационные события — не наша ответственность.
        telemetry=False,
        # Своего файла сессий у адаптера нет: сессии — секреты, и хранит их
        # ядро, в одном месте и зашифрованными.
        store=runtime.store,
    )
    if envelope is not None and envelope.mt_instance_id:
        extra.mt_instance_id = envelope.mt_instance_id
    extra.user_agent = _user_agent(extra, web=web, envelope=envelope)
    runtime.extra = extra
    return extra


def _user_agent(extra: Any, *, web: bool, envelope: Envelope | None) -> Any:
    """Восстанавливает прежнее «устройство» аккаунта.

    Свежий отпечаток на каждое переподключение выглядел бы со стороны MAX как
    десяток разных телефонов на одном аккаунте — ровно то поведение, за которое
    аккаунты и блокируют.
    """
    fresh = extra.generate_web_user_agent() if web else extra.generate_user_agent()
    saved = envelope.user_agent if envelope else None
    if saved is None:
        return fresh
    try:
        return type(fresh).model_validate(saved)
    except Exception:
        # Прежний отпечаток не читается: подключение важнее маскировки.
        return fresh


def session_from(runtime: ClientRuntime, account_id: int, *, phone: str, kind: str) -> Session:
    """Собирает доменную сессию из того, что PyMax отдал хранилищу."""
    saved = runtime.store.saved
    token = getattr(saved, "token", None)
    device_id = getattr(saved, "device_id", None)
    if not isinstance(token, str) or not token or not isinstance(device_id, str):
        raise TransportAuthError("PyMax не отдал токен сессии")
    envelope = Envelope(
        kind=kind,
        token=token,
        mt_instance_id=str(getattr(saved, "mt_instance_id", "") or ""),
        user_agent=runtime.user_agent_dump(),
    )
    return Session(account_id=account_id, phone=phone, token=encode(envelope), device_id=device_id)


def as_chat_id(chat_id: str) -> int:
    """MAX адресует чаты числом; всё прочее не станет верным при повторе."""
    try:
        return int(chat_id)
    except (TypeError, ValueError) as exc:
        raise TransportPermanent(
            f"MAX ожидает числовой идентификатор чата, получено {chat_id!r}"
        ) from exc
