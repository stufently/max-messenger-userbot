"""Точка расширения: обработчики событий поверх ядра.

Контракт — в [contract][maxub.core.handlers.contract], проверки объявлений — в
[registry][maxub.core.handlers.registry], сам разбор журнала — в
[dispatcher][maxub.core.handlers.dispatcher]. Готовых обработчиков в поставке
нет: v1 фиксирует границу, а не библиотеку сценариев.
"""

from __future__ import annotations

from maxub.core.handlers.contract import EventHandler, HandlerContext, HandlerError
from maxub.core.handlers.dispatcher import HandlerActions, HandlerDispatcher
from maxub.core.handlers.registry import HandlerRegistry, HandlerRegistryError

__all__ = [
    "EventHandler",
    "HandlerActions",
    "HandlerContext",
    "HandlerDispatcher",
    "HandlerError",
    "HandlerRegistry",
    "HandlerRegistryError",
]
