"""Словарь ручного разбора: исходы, правила и события.

Отделено от [manual_retry][maxub.core.manual_retry] потому, что этим пользуются
все слои: ядро принимает решение, API отдаёт исход наружу, CLI печатает его
человеку. Держать общие понятия рядом с политикой значило бы тянуть политику в
адаптеры за одним перечислением.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel

from maxub.core.models import Event, OutboxItem

DUPLICATE_WARNING = (
    "сверить с сервером не удалось: сообщение могло дойти, повтор способен создать дубль"
)


def require_reason(reason: str) -> str:
    """Нормализует причину отказа и не пропускает пустую.

    Верхняя граница длины остаётся на входном адаптере: она защищает от
    раздутого запроса, а не выражает смысл. Здесь же живёт само правило —
    окончательное решение без объяснения не принимается, — и оно обязано
    действовать для любого вызывающего, а не только для того, кто пришёл по
    HTTP. ``ValueError``, а не отказ пользователю: до ядра такой вызов доходит
    только мимо проверенной границы, то есть по ошибке в коде.
    """
    normalized = reason.strip()
    if not normalized:
        raise ValueError("причина отказа обязательна: решение без объяснения не восстановить")
    return normalized


def discarded_event(item: OutboxItem, reason: str) -> Event:
    """Событие об отказе от записи.

    Подписчик, ждавший судьбу сообщения, обязан узнать и такой её исход: без
    события отправка, отменённая человеком, выглядела бы для него вечно
    незавершённой. Ключ дедупликации — клиентский токен сообщения, как у
    отправки: у одной записи исход ровно один.
    """
    return Event(
        account_id=item.account_id,
        kind="message.discarded",
        payload={"outbox_id": item.id, "chat_id": item.chat_id, "reason": reason},
        dedup_key=f"discarded:{item.idempotency_key}",
    )


class RetryCheck(StrEnum):
    """Чем закончилась сверка перед ручным повтором.

    «Не смогли спросить» разбито на два значения намеренно: нет соединения —
    состояние временное и стоит повторить позже, неумение транспорта не пройдёт
    никогда. Человеку это говорит разное, а по одному слову «не удалось» он бы
    не отличил одно от другого.
    """

    FOUND = "found"
    NOT_FOUND = "not_found"
    INCONCLUSIVE = "inconclusive"
    NO_CONNECTION = "no_connection"
    UNSUPPORTED = "unsupported"


class ManualRetryResult(BaseModel):
    """Исход ручного разбора — одинаковый для человека и для скрипта.

    ``duplicate_risk`` выделен отдельным полем, а не оставлен в тексте: скрипту
    нужен признак, который можно проверить, а не строка, которую надо читать.
    """

    requeued: bool
    check: RetryCheck
    duplicate_risk: bool
    detail: str | None = None
    item: OutboxItem


class OutboxItemNotFound(Exception):
    """Записи с таким идентификатором нет."""


class OutboxItemBusy(Exception):
    """Запись не в терминальном состоянии — распоряжается ею не человек."""
