"""Команды управления токенами API.

Выпущенный токен печатается ровно один раз: демон хранит только отпечаток и
повторить выдачу не может. Потерявший токен выпускает новый и отзывает старый.
"""

from __future__ import annotations

from typing import Annotated

import typer
from rich.table import Table

from maxub.cli.context import client_of, emit
from maxub.core.permissions import ALL_SCOPES

app = typer.Typer(no_args_is_help=True, help="Токены доступа к API.")

SCOPES_HELP = "Область доступа; параметр повторяется. Доступны: " + ", ".join(
    sorted(scope.value for scope in ALL_SCOPES)
)


@app.command("list")
def tokens_list(
    ctx: typer.Context,
    include_revoked: Annotated[
        bool, typer.Option("--include-revoked", help="Показать и отозванные.")
    ] = False,
) -> None:
    """Список выпущенных токенов."""
    data = client_of(ctx).get("/tokens", params={"include_revoked": include_revoked})
    table = Table("id", "метка", "права", "истекает", "использован", "отозван")
    for item in data:
        table.add_row(
            str(item["id"]),
            item["label"],
            " ".join(sorted(item["scopes"])),
            item.get("expires_at") or "бессрочно",
            item.get("last_used_at") or "-",
            item.get("revoked_at") or "-",
        )
    emit(ctx, data, table)


@app.command("add")
def tokens_add(
    ctx: typer.Context,
    label: Annotated[str, typer.Option(help="Чей это токен и зачем.")],
    scope: Annotated[list[str], typer.Option("--scope", help=SCOPES_HELP)],
    expires_in_days: Annotated[
        int | None, typer.Option("--expires-in-days", help="Срок жизни в днях.")
    ] = None,
) -> None:
    """Выпустить токен. Значение показывается один раз."""
    emit(
        ctx,
        client_of(ctx).post(
            "/tokens",
            json={"label": label, "scopes": scope, "expires_in_days": expires_in_days},
        ),
    )


@app.command("revoke")
def tokens_revoke(
    ctx: typer.Context,
    token_id: Annotated[int, typer.Option("--id", help="Идентификатор токена.")],
) -> None:
    """Отозвать токен. Действует немедленно, в том числе для открытой панели."""
    emit(ctx, client_of(ctx).request("DELETE", f"/tokens/{token_id}"))
