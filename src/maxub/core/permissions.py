"""Области доступа к локальному API.

Раньше право было одно: кто знает токен из файла — тот распоряжается всеми
аккаунтами. Пока потребитель у API один (человек со своего же компьютера), это
терпимо; как только появляется второй — скрипт в соседнем контейнере,
обработчик событий, помощник на время отладки, — «всё или ничего» означает, что
любому из них выдаётся полный доступ к переписке и к отправке от чужого имени.

Области названы по паре «предмет: действие». Чтение и запись разделены, потому
что обычная работа скрипта — читать: следить за очередью, снимать состояние,
разбирать журнал. Право отправлять сообщения от лица владельца — принципиально
другой уровень доверия, и выдаваться оно должно отдельным решением.

``admin`` намеренно не включает в себя остальные области. Соблазн «админ может
всё» велик, но тогда токен, выданный ради одного лишь выпуска других токенов,
молча получал бы и доступ к переписке. Кому нужно и то и другое — перечисляет
обе области явно; лишнего в наборе прав быть не должно по определению.

``events:read`` покрывает журнал событий целиком, а в нём лежат тексты входящих
сообщений, поэтому маршруты журнала требуют вдобавок ``messages:read``: право
читать переписку не должно доставаться в довесок к праву следить за состоянием.
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum


class Scope(StrEnum):
    """Область доступа. Значение — то, что видит человек в CLI и в API."""

    ACCOUNTS_READ = "accounts:read"
    ACCOUNTS_WRITE = "accounts:write"
    MESSAGES_READ = "messages:read"
    MESSAGES_WRITE = "messages:write"
    EVENTS_READ = "events:read"
    ADMIN = "admin"


#: Полный набор. Им обладает корневой токен из файла — тот, которым демон
#: доказывает владельцу право на всё остальное.
ALL_SCOPES: frozenset[Scope] = frozenset(Scope)


class UnknownScopeError(ValueError):
    """Область написана с ошибкой или больше не существует.

    Молча пропустить незнакомое имя нельзя: опечатка в `messages:wrte` тогда
    выдала бы токен без права отправки, и разбираться пришлось бы по отказу в
    рантайме, а не по отказу на выпуске.
    """

    def __init__(self, name: str) -> None:
        known = ", ".join(sorted(scope.value for scope in ALL_SCOPES))
        super().__init__(f"неизвестная область доступа {name!r}, доступны: {known}")
        self.name = name


def parse_scopes(values: Iterable[str]) -> frozenset[Scope]:
    """Разбирает список областей, отвергая незнакомые."""
    scopes: set[Scope] = set()
    for raw in values:
        name = raw.strip()
        if not name:
            continue
        try:
            scopes.add(Scope(name))
        except ValueError as exc:
            raise UnknownScopeError(name) from exc
    return frozenset(scopes)


def format_scopes(scopes: Iterable[Scope]) -> str:
    """Область через пробел — вид для хранения в БД и для показа человеку."""
    return " ".join(sorted(scope.value for scope in scopes))
