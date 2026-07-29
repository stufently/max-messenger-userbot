"""Реестр обработчиков и проверка их объявлений.

Проверки идут при сборке, а не при первом событии: обработчик с опечаткой в
названии возможности или с чужим именем должен ронять запуск демона, а не тихо
простаивать месяцами. Тихий простой — худший из возможных исходов: он выглядит
ровно как «событий не было».
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from maxub.core.handlers.contract import EventHandler
from maxub.transport.base import Capabilities

#: Имена полей `Capabilities` — единственный допустимый словарь для `requires`.
KNOWN_CAPABILITIES: frozenset[str] = frozenset(Capabilities.model_fields)


class HandlerRegistryError(ValueError):
    """Обработчик объявлен неправильно и в реестр не попадёт."""


class HandlerRegistry:
    """Обработчики этой сборки демона."""

    def __init__(self, handlers: Iterable[EventHandler] = ()) -> None:
        self._handlers: list[EventHandler] = []
        for handler in handlers:
            self.add(handler)

    def add(self, handler: EventHandler) -> None:
        name = getattr(handler, "name", "")
        if not name:
            raise HandlerRegistryError("у обработчика должно быть непустое имя")
        if any(existing.name == name for existing in self._handlers):
            raise HandlerRegistryError(
                f"обработчик {name!r} уже зарегистрирован: имя — ключ его позиции в журнале,"
                " и два обработчика с одним именем делили бы один курсор"
            )
        unknown = sorted(set(handler.requires) - KNOWN_CAPABILITIES)
        if unknown:
            known = ", ".join(sorted(KNOWN_CAPABILITIES))
            raise HandlerRegistryError(
                f"обработчик {name!r} требует неизвестных возможностей {unknown}; известны: {known}"
            )
        self._handlers.append(handler)

    def wants(self, handler: EventHandler, kind: str) -> bool:
        return not handler.kinds or kind in handler.kinds

    @property
    def handlers(self) -> Sequence[EventHandler]:
        return tuple(self._handlers)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(handler.name for handler in self._handlers)

    def __bool__(self) -> bool:
        return bool(self._handlers)

    def __len__(self) -> int:
        return len(self._handlers)
