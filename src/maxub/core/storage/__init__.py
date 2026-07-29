"""Прикладная БД.

Собрана из частей по зонам ответственности: подключение и миграции, аккаунты с
сессиями, постановка в очередь, жизненный цикл доставки, штрафы лимитера,
журнал событий. Наружу отдаётся один объект — вызывающему коду не нужно знать
про это деление.
"""

from __future__ import annotations

from maxub.core.storage.accounts import AccountsMixin
from maxub.core.storage.base import Database, DuplicateAccountError
from maxub.core.storage.events import EventsMixin
from maxub.core.storage.outbox import OutboxMixin
from maxub.core.storage.penalties import PenaltiesMixin
from maxub.core.storage.review import ReviewMixin


class Storage(
    AccountsMixin,
    OutboxMixin,
    # Решения человека наследуют обычный жизненный цикл доставки, поэтому
    # `DeliveryMixin` приходит вместе с ними и отдельно не перечисляется.
    ReviewMixin,
    PenaltiesMixin,
    EventsMixin,
    Database,
):
    """Единая точка доступа к данным демона."""


__all__ = ["DuplicateAccountError", "Storage"]
