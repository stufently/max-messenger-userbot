"""CLI: тонкий клиент локального API.

Два входа в одно и то же приложение:

* ``maxub``    — человеческий режим, таблицы Rich;
* ``maxubctl`` — машинный режим, JSON по умолчанию. Рассчитан на вызов из
  скриптов и агентами внутри контейнера: токен подхватывается из ``/data``,
  ничего интерактивного, стабильные коды выхода.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

import click
import typer
from rich.console import Console
from rich.table import Table

from maxub.cli.client import EXIT_OK, EXIT_USAGE, ApiClient, ApiError
from maxub.config import ClientSettings, Settings

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Управление демоном MAX Userbot.",
)
accounts_app = typer.Typer(no_args_is_help=True, help="Аккаунты.")
login_app = typer.Typer(no_args_is_help=True, help="Авторизация аккаунта.")
app.add_typer(accounts_app, name="accounts")
app.add_typer(login_app, name="login")

stdout = Console()
stderr = Console(stderr=True)


@dataclass
class Context:
    client: ApiClient
    json_output: bool


def _ctx(ctx: typer.Context) -> Context:
    obj: Context = ctx.obj
    return obj


def _emit(ctx: typer.Context, data: Any, table: Table | None = None) -> None:
    if _ctx(ctx).json_output or table is None:
        stdout.print_json(json.dumps(data, ensure_ascii=False, default=str))
    else:
        stdout.print(table)


def _fail(message: str, exit_code: int) -> None:
    """Завершает процесс кодом, который разбирает вызывающий скрипт.

    Используется SystemExit, а не typer.Exit: последний перехватывается только
    внутри Click, а сюда мы попадаем уже за пределами его обработчика.
    """
    stderr.print(f"[red]{message}[/red]")
    raise SystemExit(exit_code)


@app.callback()
def main(
    ctx: typer.Context,
    url: Annotated[str | None, typer.Option(help="Базовый URL демона.")] = None,
    token: Annotated[str | None, typer.Option(help="Токен API.")] = None,
    data_dir: Annotated[str | None, typer.Option(help="Каталог данных для поиска токена.")] = None,
    timeout: Annotated[float | None, typer.Option(help="Таймаут запроса, секунды.")] = None,
    json_output: Annotated[
        bool | None,
        typer.Option("--json/--no-json", help="Машиночитаемый вывод."),
    ] = None,
) -> None:
    settings = ClientSettings()
    if data_dir:
        settings.data_dir = Path(data_dir)
    if url:
        settings.url = url
    if token:
        settings.token = token
    if timeout is not None:
        settings.timeout = timeout

    resolved_json = json_output if json_output is not None else os.getenv("MAXUB_JSON") == "1"
    ctx.obj = Context(
        client=ApiClient(settings.base_url, settings.resolve_token(), settings.timeout),
        json_output=resolved_json,
    )


@app.command()
def daemon() -> None:
    """Запустить демона (блокирующий вызов)."""
    from maxub.daemon import run

    run(Settings())


@app.command()
def token() -> None:
    """Показать токен API — им пользуются клиенты внутри того же контейнера."""
    value = Settings().resolve_token()
    stdout.print(value)


@app.command()
def status(ctx: typer.Context) -> None:
    """Состояние демона: аккаунты, соединения, очередь."""
    data = _ctx(ctx).client.get("/status")
    table = Table("параметр", "значение")
    for key, value in data.items():
        table.add_row(
            key, json.dumps(value, ensure_ascii=False) if isinstance(value, dict) else str(value)
        )
    _emit(ctx, data, table)


@accounts_app.command("list")
def accounts_list(ctx: typer.Context) -> None:
    """Список аккаунтов."""
    data = _ctx(ctx).client.get("/accounts")
    table = Table("id", "телефон", "метка", "состояние", "ошибка")
    for item in data:
        table.add_row(
            str(item["id"]),
            item["phone"],
            item.get("label") or "-",
            item["state"],
            item.get("last_error") or "-",
        )
    _emit(ctx, data, table)


@accounts_app.command("add")
def accounts_add(
    ctx: typer.Context,
    phone: Annotated[str, typer.Option(help="Номер телефона аккаунта.")],
    label: Annotated[str | None, typer.Option(help="Произвольная метка.")] = None,
) -> None:
    """Добавить аккаунт."""
    data = _ctx(ctx).client.post("/accounts", json={"phone": phone, "label": label})
    _emit(ctx, data)


@accounts_app.command("disable")
def accounts_disable(
    ctx: typer.Context,
    account_id: Annotated[int, typer.Option("--account-id", help="Идентификатор аккаунта.")],
    reason: Annotated[str, typer.Option(help="Причина остановки.")] = "остановлен вручную",
) -> None:
    """Остановить аккаунт и разорвать соединение."""
    data = _ctx(ctx).client.post(f"/accounts/{account_id}/disable", json={"reason": reason})
    _emit(ctx, data)


@login_app.command("start")
def login_start(
    ctx: typer.Context,
    account_id: Annotated[int, typer.Option("--account-id", help="Идентификатор аккаунта.")],
) -> None:
    """Запросить код подтверждения."""
    data = _ctx(ctx).client.post("/login/start", json={"account_id": account_id})
    _emit(ctx, data)


@login_app.command("complete")
def login_complete(
    ctx: typer.Context,
    challenge_id: Annotated[str, typer.Option("--challenge-id", help="Идентификатор запроса.")],
    code: Annotated[str, typer.Option(help="Код подтверждения.")],
) -> None:
    """Подтвердить код и подключить аккаунт."""
    data = _ctx(ctx).client.post(
        "/login/complete", json={"challenge_id": challenge_id, "code": code}
    )
    _emit(ctx, data)


@app.command()
def send(
    ctx: typer.Context,
    account_id: Annotated[int, typer.Option("--account-id", help="Идентификатор аккаунта.")],
    chat_id: Annotated[str, typer.Option("--chat-id", help="Идентификатор чата.")],
    text: Annotated[str, typer.Option(help="Текст сообщения.")],
    nonce: Annotated[
        str | None,
        typer.Option(help="Отличает намеренный повтор от дубля при ретрае."),
    ] = None,
) -> None:
    """Поставить сообщение в очередь отправки."""
    data = _ctx(ctx).client.post(
        "/send",
        json={"account_id": account_id, "chat_id": chat_id, "text": text, "nonce": nonce},
    )
    _emit(ctx, data)


@app.command()
def history(
    ctx: typer.Context,
    account_id: Annotated[int, typer.Option("--account-id", help="Идентификатор аккаунта.")],
    chat_id: Annotated[str, typer.Option("--chat-id", help="Идентификатор чата.")],
    limit: Annotated[int, typer.Option(help="Сколько сообщений вернуть.")] = 20,
) -> None:
    """Выгрузить историю чата."""
    data = _ctx(ctx).client.get(
        "/history", params={"account_id": account_id, "chat_id": chat_id, "limit": limit}
    )
    table = Table("id", "чат", "исходящее", "текст")
    for item in data:
        table.add_row(
            item["remote_id"], item["chat_id"], str(item["outgoing"]), item.get("text") or ""
        )
    _emit(ctx, data, table)


@app.command()
def events(
    ctx: typer.Context,
    limit: Annotated[int, typer.Option(help="Сколько событий вернуть.")] = 50,
    after_id: Annotated[
        int, typer.Option("--after-id", help="Вернуть события после этого id.")
    ] = 0,
) -> None:
    """Показать события. Курсор `--after-id` позволяет читать без дублей."""
    data = _ctx(ctx).client.get("/events", params={"limit": limit, "after_id": after_id})
    table = Table("id", "аккаунт", "тип", "данные")
    for item in data:
        table.add_row(
            str(item["id"]),
            str(item.get("account_id") or "-"),
            item["kind"],
            json.dumps(item.get("payload", {}), ensure_ascii=False),
        )
    _emit(ctx, data, table)


@app.command()
def shutdown(ctx: typer.Context) -> None:
    """Остановить демона."""
    data = _ctx(ctx).client.post("/shutdown")
    _emit(ctx, data)


def _run(argv: list[str] | None = None) -> None:
    try:
        app(args=argv, standalone_mode=False)
    except ApiError as exc:
        _fail(str(exc), exc.exit_code)
    except (typer.Exit, click.exceptions.Exit) as exc:
        raise SystemExit(exc.exit_code) from exc
    except click.ClickException as exc:
        stderr.print(f"[red]{exc.format_message()}[/red]")
        raise SystemExit(EXIT_USAGE) from exc
    except click.exceptions.Abort as exc:
        raise SystemExit(EXIT_USAGE) from exc
    raise SystemExit(EXIT_OK)


def ctl() -> None:
    """Точка входа `maxubctl`: JSON по умолчанию."""
    os.environ.setdefault("MAXUB_JSON", "1")
    _run(sys.argv[1:])


def cli() -> None:
    """Точка входа `maxub`."""
    _run(sys.argv[1:])
