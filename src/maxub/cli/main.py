"""CLI: тонкий клиент локального API.

Два входа в одно и то же приложение:

* ``maxub``    — человеческий режим, таблицы Rich;
* ``maxubctl`` — машинный режим, JSON по умолчанию. Рассчитан на вызов из
  скриптов и агентами внутри контейнера: токен подхватывается из каталога
  данных, ничего интерактивного, стабильные коды выхода.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Annotated

import typer

from maxub.cli import errors
from maxub.cli.client import EXIT_ERROR, EXIT_OK, EXIT_USAGE, ApiClient, ApiError
from maxub.cli.commands import accounts, login, messages, tokens
from maxub.cli.context import Context, fail, stdout
from maxub.config import ClientSettings, Settings
from maxub.paths import DataDirError

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Управление демоном MAX Userbot.",
)
app.add_typer(accounts.app, name="accounts")
app.add_typer(login.app, name="login")
app.add_typer(tokens.app, name="tokens")

app.command()(messages.status)
app.command()(messages.send)
app.command()(messages.outbox)
app.command()(messages.retry)
app.command()(messages.discard)
app.command()(messages.history)
app.command()(messages.events)
app.command()(messages.capabilities)
app.command()(messages.shutdown)


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
    """Показать корневой токен API — им пользуются клиенты в том же контейнере.

    Корневой токен даёт все права. Скрипту, которому нужно меньше, выпускается
    свой: `maxub tokens add --label ... --scope messages:read`.
    """
    stdout.print(Settings().resolve_token())


def _run(argv: list[str] | None = None) -> None:
    try:
        app(args=argv, standalone_mode=False)
    except ApiError as exc:
        fail(str(exc), exc.exit_code)
    except DataDirError as exc:
        # Каталог данных недоступен — это не ошибка вызова (код 2), а отказ на
        # старте: сообщение уже написано словами, печатать к нему трассировку
        # значит прятать его в шуме.
        fail(str(exc), EXIT_ERROR)
    except errors.EXITS as exc:
        raise SystemExit(errors.exit_code_of(exc, EXIT_OK)) from exc
    except errors.USAGE as exc:
        errors.show(exc)
        raise SystemExit(errors.exit_code_of(exc, EXIT_USAGE)) from exc
    except errors.ABORTS as exc:
        raise SystemExit(EXIT_USAGE) from exc
    raise SystemExit(EXIT_OK)


def ctl() -> None:
    """Точка входа `maxubctl`: JSON по умолчанию."""
    os.environ.setdefault("MAXUB_JSON", "1")
    _run(sys.argv[1:])


def cli() -> None:
    """Точка входа `maxub`."""
    _run(sys.argv[1:])
