"""Прикладная БД.

Собрана из частей по зонам ответственности: подключение и миграции, аккаунты с
сессиями, постановка в очередь, жизненный цикл доставки, штрафы лимитера,
журнал событий. Наружу отдаётся один объект — вызывающему коду не нужно знать
про это деление.
"""

from __future__ import annotations

from maxub.core.storage.accounts import AccountsMixin
from maxub.core.storage.base import Database, DuplicateAccountError
from maxub.core.storage.delivery import DeliveryMixin
from maxub.core.storage.events import EventsMixin
from maxub.core.storage.outbox import OutboxMixin
from maxub.core.storage.penalties import PenaltiesMixin


class Storage(
    AccountsMixin,
    OutboxMixin,
    DeliveryMixin,
    PenaltiesMixin,
    EventsMixin,
    Database,
):
    """Единая точка доступа к данным демона."""


__all__ = ["DuplicateAccountError", "Storage"]
