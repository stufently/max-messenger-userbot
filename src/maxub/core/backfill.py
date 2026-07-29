"""Добор событий, пропущенных за время простоя.

Вынесен из [sync][maxub.core.sync] отдельно: у добора своя политика (курсор
обязан продвигаться, незавершённость обязана быть видимой), и в надзорном цикле
она только мешала бы читать логику переподключения.
"""

from __future__ import annotations

import logging

from maxub.core.models import AccountState, Event, Message
from maxub.core.ports import AccountRepository, EventSink
from maxub.transport.base import Transport, TransportError

log = logging.getLogger(__name__)

PAGE_SIZE = 100

#: После этого числа страниц добор считается затянувшимся и о нём сообщается.
#: Это порог видимости, а не предел: обрывать добор по счётчику нельзя, иначе
#: хвост пропущенного молча теряется.
SLOW_AFTER_PAGES = 100


class BackfillStalled(TransportError):
    """Транспорт не продвинул курсор, хотя порция непустая.

    Контракт `fetch_updates` требует продвижения. Продолжать в такой ситуации
    значит бесконечно перечитывать одну и ту же страницу, поэтому добор
    прекращается, а ошибка доходит до состояния аккаунта.
    """


def received_event(account_id: int, message: Message) -> Event:
    """Событие о входящем сообщении.

    Живой поток и добор публикуют его одинаково и с одним ключом дедупликации:
    их порции намеренно пересекаются, и различие ключей превратило бы это
    пересечение в дубликаты.
    """
    return Event(
        account_id=account_id,
        kind="message.received",
        payload=message.model_dump(mode="json"),
        dedup_key=f"recv:{account_id}:{message.remote_id}",
    )


class Backfiller:
    """Догоняет журнал сервера с последней сохранённой позиции."""

    def __init__(self, repo: AccountRepository, emit: EventSink) -> None:
        self._repo = repo
        self._emit = emit

    async def run(self, account_id: int, transport: Transport) -> int:
        """Добирает всё пропущенное, сдвигая позицию после каждой страницы.

        Порядок внутри страницы — сначала события, потом курсор: при падении
        между ними страница будет перечитана и отсеяна дедупликацией, тогда как
        обратный порядок потерял бы её насовсем.

        Лимит страниц больше не заканчивает добор: раньше аккаунт после лимита
        уходил в `READY`, будто всё догнано, и хвост пропущенного пропадал из
        виду. Теперь на пороге публикуется `account.backfill_incomplete`, а
        добор продолжается. Долговременный след — именно событие: пояснение в
        `last_error` живёт только пока аккаунт в `SYNCING`, переход в `READY`
        его затирает, и это правильно — незавершённости к тому моменту нет. От
        вечного цикла защищает не счётчик страниц, а требование к курсору
        продвигаться.
        """
        if not transport.capabilities.backfill:
            return 0
        cursor = await self._repo.load_cursor(account_id)
        pages = 0
        total = 0
        while True:
            updates, next_cursor = await transport.fetch_updates(cursor, PAGE_SIZE)
            if not updates:
                return total
            if next_cursor is None or next_cursor == cursor:
                raise BackfillStalled(
                    f"транспорт вернул {len(updates)} событий, не сдвинув курсор {cursor!r}"
                )
            for update in updates:
                await self._emit(received_event(account_id, update.message))
            total += len(updates)
            cursor = next_cursor
            await self._repo.save_cursor(account_id, cursor)
            pages += 1
            if pages == SLOW_AFTER_PAGES:
                await self._report_slow(account_id, cursor, pages, total)

    async def _report_slow(self, account_id: int, cursor: str, pages: int, total: int) -> None:
        """Делает затянувшийся добор видимым, не останавливая его."""
        detail = f"добор ещё идёт: {pages} страниц, {total} событий, позиция {cursor}"
        log.warning("аккаунт %s: %s", account_id, detail)
        # Состояние остаётся SYNCING — меняется только пояснение, чтобы причина
        # долгого ожидания читалась в статусе аккаунта, а не только в логах.
        await self._repo.set_account_state(account_id, AccountState.SYNCING, detail)
        await self._emit(
            Event(
                account_id=account_id,
                kind="account.backfill_incomplete",
                payload={"pages": pages, "events": total, "cursor": cursor},
                dedup_key=f"backfill-incomplete:{account_id}:{cursor}",
            )
        )
