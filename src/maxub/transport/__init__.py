"""Реестр транспортных адаптеров."""

from __future__ import annotations

from collections.abc import Callable

from maxub.transport.base import Capabilities, Transport, TransportError
from maxub.transport.stub import StubTransport

TransportFactory = Callable[[], Transport]


def _pymax() -> Transport:
    """Создаёт адаптер PyMax, не втягивая библиотеку в импорт реестра.

    Импорт отложен до самой попытки создать транспорт: без установленного
    PyMax платформа обязана подниматься как раньше на `stub`, а сам выбор
    `pymax` — объяснять, чего не хватает.
    """
    from maxub.transport.pymax import PyMaxTransport

    return PyMaxTransport()


_REGISTRY: dict[str, TransportFactory] = {
    "stub": StubTransport,
    "pymax": _pymax,
}


def available() -> list[str]:
    return sorted(_REGISTRY)


def get_factory(name: str) -> TransportFactory:
    try:
        return _REGISTRY[name]
    except KeyError:
        raise TransportError(
            f"неизвестный транспорт {name!r}, доступны: {', '.join(available())}"
        ) from None


def register(name: str, factory: TransportFactory) -> None:
    _REGISTRY[name] = factory


__all__ = [
    "Capabilities",
    "Transport",
    "TransportError",
    "TransportFactory",
    "available",
    "get_factory",
    "register",
]
