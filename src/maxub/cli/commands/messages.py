"""Команды работы с сообщениями и событиями."""

from __future__ import annotations

import json
from typing import Annotated

import typer
from rich.table import Table

from maxub.cli.context import client_of, emit

ACCOUNT_OPTION = typer.Option("--account-id", help="Идентификатор аккаунта.")
CHAT_OPTION = typer.Option("--chat-id", help="Идентификатор чата.")


def send(
    ctx: typer.Context,
    account_id: Annotated[int, ACCOUNT_OPTION],
    chat_id: Annotated[str, CHAT_OPTION],
    text: Annotated[str, typer.Option(help="Текст сообщения.")],
    nonce: Annotated[
        str | None,
        typer.Option(help="Отличает намеренный повтор от дубля при ретрае."),
    ] = None,
) -> None:
    """Поставить сообщение в очередь отправки."""
    emit(
        ctx,
        client_of(ctx).post(
            "/send",
            json={"account_id": account_id, "chat_id": chat_id, "text": text, "nonce": nonce},
        ),
    )


def outbox(
    ctx: typer.Context,
    state: Annotated[
        str | None,
        typer.Option(help="Только это состояние: failed или sending."),
    ] = None,
    limit: Annotated[int, typer.Option(help="Сколько записей вернуть.")] = 50,
) -> None:
    """Показать записи очереди, которые ждут решения человека.

    Без `--state` это отказавшие и застрявшие «в полёте»: остальные движутся
    сами. Идентификатор из первого столбца принимает команда `retry`.
    """
    params: dict[str, object] = {"limit": limit}
    if state:
        params["state"] = state
    data = client_of(ctx).get("/outbox", params=params)
    table = Table("id", "аккаунт", "чат", "состояние", "попыток", "создано", "ошибка")
    for item in data:
        table.add_row(
            str(item["id"]),
            str(item["account_id"]),
            item["chat_id"],
            item["state"],
            str(item["attempts"]),
            str(item["created_at"]),
            item.get("error") or "",
        )
    emit(ctx, data, table)


def retry(
    ctx: typer.Context,
    item_id: Annotated[int, typer.Option("--id", help="Идентификатор записи очереди.")],
) -> None:
    """Повторить отправку отказавшей записи.

    ОСТОРОЖНО: сообщение могло дойти до получателя, и тогда повтор создаст у
    него дубль. Перед повтором демон сверяется с сервером, если транспорт это
    умеет; когда сверить не удалось, ответ помечен `duplicate_risk: true` — риск
    берёт на себя тот, кто выполняет команду.

    Повторить можно любую запись в состоянии failed — и с неизвестным исходом, и
    с исчерпанными попытками, и отвергнутую после потери авторизации. Живую
    запись у демона забирать нельзя: такой вызов завершается кодом 5.
    """
    emit(ctx, client_of(ctx).post(f"/outbox/{item_id}/retry"))


def history(
    ctx: typer.Context,
    account_id: Annotated[int, ACCOUNT_OPTION],
    chat_id: Annotated[str, CHAT_OPTION],
    limit: Annotated[int, typer.Option(help="Сколько сообщений вернуть.")] = 20,
) -> None:
    """Выгрузить историю чата."""
    data = client_of(ctx).get(
        "/history", params={"account_id": account_id, "chat_id": chat_id, "limit": limit}
    )
    table = Table("id", "чат", "исходящее", "текст")
    for item in data:
        table.add_row(
            item["remote_id"], item["chat_id"], str(item["outgoing"]), item.get("text") or ""
        )
    emit(ctx, data, table)


def events(
    ctx: typer.Context,
    limit: Annotated[int, typer.Option(help="Сколько событий вернуть.")] = 50,
    after_id: Annotated[
        int, typer.Option("--after-id", help="Вернуть события после этого id.")
    ] = 0,
) -> None:
    """Показать события. Курсор `--after-id` позволяет читать без дублей."""
    data = client_of(ctx).get("/events", params={"limit": limit, "after_id": after_id})
    table = Table("id", "аккаунт", "тип", "данные")
    for item in data:
        table.add_row(
            str(item["id"]),
            str(item.get("account_id") or "-"),
            item["kind"],
            json.dumps(item.get("payload", {}), ensure_ascii=False),
        )
    emit(ctx, data, table)


def capabilities(
    ctx: typer.Context,
    account_id: Annotated[int, ACCOUNT_OPTION],
) -> None:
    """Показать, что умеет транспорт этого аккаунта."""
    data = client_of(ctx).get(f"/accounts/{account_id}/capabilities")
    table = Table("возможность", "доступна")
    for key, value in data.items():
        table.add_row(key, "да" if value else "нет")
    emit(ctx, data, table)


def status(ctx: typer.Context) -> None:
    """Состояние демона: аккаунты, соединения, очередь."""
    data = client_of(ctx).get("/status")
    table = Table("параметр", "значение")
    for key, value in data.items():
        rendered = json.dumps(value, ensure_ascii=False) if isinstance(value, dict) else str(value)
        table.add_row(key, rendered)
    emit(ctx, data, table)


def shutdown(ctx: typer.Context) -> None:
    """Остановить демона."""
    emit(ctx, client_of(ctx).post("/shutdown"))
