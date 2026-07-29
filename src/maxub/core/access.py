"""Кто обращается к API и что ему разрешено.

Два вида предъявителя. Корневой токен лежит в файле каталога данных, имеет все
права и через API не отзывается: это ключ владельца машины, и отозвать его
можно только тем же способом, каким он появился, — удалив файл. Все остальные
токены выпускаются самим владельцем, лежат в базе отпечатками и отзываются по
одному.

Разделение существует ради простой вещи: право читать состояние и право
отправлять сообщения от чужого имени не должны выдаваться одним движением.
Список областей — в [permissions][maxub.core.permissions].
"""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel

from maxub.core.models import ApiToken, utcnow
from maxub.core.permissions import ALL_SCOPES, Scope
from maxub.core.ports import TokenRepository

#: Длина выпускаемого секрета в байтах до кодирования. Столько же у корневого
#: токена: перебор по сети бессмыслен при любой из этих длин, а разнобой в
#: длине только намекал бы, что какой-то из токенов слабее.
TOKEN_BYTES = 32

#: Как подписан корневой токен в списках и в журнале.
ROOT_LABEL = "корневой"


def fingerprint(raw: str) -> str:
    """Отпечаток токена — то, что хранится вместо самого токена."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class Principal(BaseModel):
    """Предъявитель токена и его права на время запроса."""

    kind: Literal["root", "token"]
    label: str
    scopes: frozenset[Scope]
    token_id: int | None = None

    def allows(self, required: Iterable[Scope]) -> bool:
        return set(required).issubset(self.scopes)

    def missing(self, required: Iterable[Scope]) -> list[Scope]:
        return sorted(set(required) - self.scopes)


class AccessControl:
    """Проверка токенов и их выпуск."""

    def __init__(self, repo: TokenRepository, root_token: str) -> None:
        self._repo = repo
        self._root_token = root_token

    async def authenticate(self, raw: str | None) -> Principal | None:
        """Опознаёт предъявителя; ``None`` — токен не годится.

        Корневой токен сравнивается постоянным по времени сравнением: он лежит
        в файле целиком, и посимвольная утечка времени сравнения была бы
        настоящей утечкой. Выпущенные токены ищутся по отпечатку — там сравнивать
        нечего, из отпечатка исходный токен не восстанавливается, а поиск по
        уникальному индексу не зависит от того, насколько предъявленное значение
        похоже на сохранённое.
        """
        if not raw:
            return None
        candidate = raw.strip()
        if not candidate:
            return None
        # Байты, а не строки: `compare_digest` на строках с не-ASCII символами
        # бросает TypeError, и подобранный заголовок валил бы запрос в 500
        # вместо честного отказа.
        if secrets.compare_digest(candidate.encode("utf-8"), self._root_token.encode("utf-8")):
            return Principal(kind="root", label=ROOT_LABEL, scopes=ALL_SCOPES)
        token = await self._repo.find_token_by_hash(fingerprint(candidate))
        if token is None or not token.is_active():
            return None
        await self._repo.touch_token(token.id)
        return Principal(kind="token", label=token.label, scopes=token.scopes, token_id=token.id)

    async def refresh(self, principal: Principal) -> Principal | None:
        """Перепроверяет уже опознанного предъявителя.

        Нужно там, где право пережило сам запрос: сессия браузера живёт часами,
        и отозванный за это время токен обязан закрыть её, а не дожить до конца
        срока сессии. Права перечитываются из базы — сокращённый набор действует
        сразу, а не со следующего входа.
        """
        if principal.kind == "root":
            return principal
        if principal.token_id is None:
            return None
        token = await self._repo.get_token(principal.token_id)
        if token is None or not token.is_active():
            return None
        return Principal(kind="token", label=token.label, scopes=token.scopes, token_id=token.id)

    async def issue(
        self, label: str, scopes: frozenset[Scope], expires_at: datetime | None = None
    ) -> tuple[str, ApiToken]:
        """Выпускает токен и отдаёт его ровно один раз.

        Сырое значение возвращается вызывающему и больше нигде не появляется: в
        базу уходит только отпечаток.
        """
        raw = secrets.token_urlsafe(TOKEN_BYTES)
        token = await self._repo.create_token(label, fingerprint(raw), scopes, expires_at)
        return raw, token

    async def revoke(self, token_id: int) -> bool:
        return await self._repo.revoke_token(token_id)

    async def list_tokens(self, include_revoked: bool = False) -> list[ApiToken]:
        return await self._repo.list_tokens(include_revoked)

    @staticmethod
    def expiry(days: int | None, now: datetime | None = None) -> datetime | None:
        """Момент истечения по сроку в днях; ``None`` — бессрочно."""
        if days is None:
            return None
        return (now or utcnow()) + timedelta(days=days)
