"""Исключения командной строки во всех копиях click, какие есть в окружении.

typer начиная с 0.16 везёт собственную копию click внутри пакета
(`typer._click`). Её исключения не наследуются от классов click, установленного
рядом отдельным пакетом: это два независимых дерева классов с одинаковыми
именами. Поэтому `except click.ClickException` не ловил ничего из того, что
бросает typer, — забытая обязательная опция вылетала трейсбеком с кодом 1 вместо
внятного сообщения с кодом 2, хотя код 2 объявлен в README частью контракта.

Обе копии берутся намеренно. Вендорная — потому что через неё сегодня проходят
все ошибки разбора аргументов. Внешняя — потому что на typer без вендоринга
существует только она, а верхняя граница на typer в проекте не закреплена: с
откатом или скачком версии код обязан работать без правок.
"""

from __future__ import annotations

from types import ModuleType
from typing import Any

import click
import typer

_FLAVOURS: tuple[ModuleType, ...]
try:
    from typer import _click as _vendored
except ImportError:  # pragma: no cover — typer, который click ещё не вендорит
    _FLAVOURS = (click,)
else:
    _FLAVOURS = (click, _vendored)


def _gather(name: str, *extra: type[BaseException]) -> tuple[type[Any], ...]:
    """Собирает одноимённый класс исключения из всех известных копий click.

    Отсутствие имени в какой-то из копий — не ошибка: набор классов между
    версиями click менялся, а нам нужны те, что есть. Дубликаты отбрасываются,
    потому что `except` с повторяющимся классом в кортеже работает, но читается
    как недосмотр.
    """
    found: list[type[Any]] = []
    namespaces = [getattr(module, "exceptions", module) for module in _FLAVOURS]
    for candidate in [*(getattr(space, name, None) for space in namespaces), *extra]:
        if not isinstance(candidate, type) or not issubclass(candidate, BaseException):
            continue
        if candidate not in found:
            found.append(candidate)
    return tuple(found)


# Сигнал «отработали, выходим» — его бросает и `--help`, и `typer.Exit` в теле
# команды. Ошибкой не является, код берётся из самого исключения.
EXITS = _gather("Exit", typer.Exit)
# Ошибки разбора аргументов: неизвестная команда, пропущенная обязательная
# опция, непреобразуемое значение, вызов группы без подкоманды.
USAGE = _gather("ClickException")
# Ctrl-C на интерактивном запросе.
ABORTS = _gather("Abort", typer.Abort)


def exit_code_of(exc: BaseException, default: int) -> int:
    """Код выхода, объявленный самим исключением click."""
    code = getattr(exc, "exit_code", None)
    return int(code) if isinstance(code, int) else default


def show(exc: BaseException) -> None:
    """Печатает сообщение штатным способом click.

    Не `format_message()`: у разных подклассов вывод разный и содержательный —
    ошибка аргумента добавляет строку `Usage:` и подсказку про `--help`, а вызов
    группы без подкоманды печатает справку целиком. Своё форматирование это
    потеряло бы.
    """
    printer = getattr(exc, "show", None)
    if callable(printer):
        printer()
