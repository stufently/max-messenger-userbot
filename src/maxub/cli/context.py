"""Общий контекст команд: клиент API и режим вывода."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from maxub.cli.client import ApiClient

stdout = Console()
stderr = Console(stderr=True)


@dataclass
class Context:
    client: ApiClient
    json_output: bool


def ctx_of(ctx: typer.Context) -> Context:
    obj: Context = ctx.obj
    return obj


def client_of(ctx: typer.Context) -> ApiClient:
    return ctx_of(ctx).client


def emit(ctx: typer.Context, data: Any, table: Table | None = None) -> None:
    """Печатает результат.

    В машинном режиме — всегда JSON. Таблица используется только когда вывод
    предназначен человеку и для команды она вообще предусмотрена.
    """
    if ctx_of(ctx).json_output or table is None:
        stdout.print_json(json.dumps(data, ensure_ascii=False, default=str))
    else:
        stdout.print(table)


def fail(message: str, exit_code: int) -> None:
    """Завершает процесс кодом, который разбирает вызывающий скрипт.

    Используется SystemExit, а не typer.Exit: последний перехватывается только
    внутри Click, а сюда мы попадаем уже за пределами его обработчика.
    """
    stderr.print(f"[red]{message}[/red]")
    raise SystemExit(exit_code)
