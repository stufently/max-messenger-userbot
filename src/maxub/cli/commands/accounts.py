"""Команды управления аккаунтами."""

from __future__ import annotations

from typing import Annotated

import typer
from rich.table import Table

from maxub.cli.context import client_of, emit

app = typer.Typer(no_args_is_help=True, help="Аккаунты.")


@app.command("list")
def accounts_list(ctx: typer.Context) -> None:
    """Список аккаунтов."""
    data = client_of(ctx).get("/accounts")
    table = Table("id", "телефон", "метка", "состояние", "ошибка")
    for item in data:
        table.add_row(
            str(item["id"]),
            item["phone"],
            item.get("label") or "-",
            item["state"],
            item.get("last_error") or "-",
        )
    emit(ctx, data, table)


@app.command("add")
def accounts_add(
    ctx: typer.Context,
    phone: Annotated[str, typer.Option(help="Номер телефона аккаунта.")],
    label: Annotated[str | None, typer.Option(help="Произвольная метка.")] = None,
) -> None:
    """Добавить аккаунт."""
    emit(ctx, client_of(ctx).post("/accounts", json={"phone": phone, "label": label}))


@app.command("disable")
def accounts_disable(
    ctx: typer.Context,
    account_id: Annotated[int, typer.Option("--account-id", help="Идентификатор аккаунта.")],
    reason: Annotated[str, typer.Option(help="Причина остановки.")] = "остановлен вручную",
) -> None:
    """Остановить аккаунт и разорвать соединение."""
    emit(ctx, client_of(ctx).post(f"/accounts/{account_id}/disable", json={"reason": reason}))
