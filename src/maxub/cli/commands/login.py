"""Команды авторизации: по телефону и по QR-коду."""

from __future__ import annotations

import sys
import time
from typing import Annotated

import qrcode
import typer

from maxub.cli.client import EXIT_CONFLICT
from maxub.cli.context import client_of, ctx_of, emit, fail, stderr

app = typer.Typer(no_args_is_help=True, help="Авторизация аккаунта.")

ACCOUNT_OPTION = typer.Option("--account-id", help="Идентификатор аккаунта.")
CHALLENGE_OPTION = typer.Option("--challenge-id", help="Идентификатор запроса.")


@app.command("start")
def login_start(ctx: typer.Context, account_id: Annotated[int, ACCOUNT_OPTION]) -> None:
    """Запросить код подтверждения на телефон."""
    emit(ctx, client_of(ctx).post("/login/start", json={"account_id": account_id}))


@app.command("complete")
def login_complete(
    ctx: typer.Context,
    challenge_id: Annotated[str, CHALLENGE_OPTION],
    code: Annotated[str, typer.Option(help="Код подтверждения.")],
) -> None:
    """Подтвердить код и подключить аккаунт."""
    emit(
        ctx,
        client_of(ctx).post("/login/complete", json={"challenge_id": challenge_id, "code": code}),
    )


@app.command("qr-start")
def login_qr_start(ctx: typer.Context, account_id: Annotated[int, ACCOUNT_OPTION]) -> None:
    """Начать вход по QR-коду и получить его содержимое."""
    data = client_of(ctx).post("/login/qr/start", json={"account_id": account_id})
    if not ctx_of(ctx).json_output:
        render_qr(data["payload"])
    emit(ctx, data)


@app.command("qr-poll")
def login_qr_poll(ctx: typer.Context, challenge_id: Annotated[str, CHALLENGE_OPTION]) -> None:
    """Проверить, подтверждён ли вход с телефона. Одна проверка, без ожидания."""
    emit(ctx, client_of(ctx).post("/login/qr/poll", json={"challenge_id": challenge_id}))


@app.command("qr")
def login_qr(
    ctx: typer.Context,
    account_id: Annotated[int, ACCOUNT_OPTION],
    timeout: Annotated[int, typer.Option(help="Сколько секунд ждать подтверждения.")] = 120,
    interval: Annotated[float, typer.Option(help="Период опроса, секунды.")] = 2.0,
) -> None:
    """Войти по QR-коду: показать код и дождаться подтверждения с телефона."""
    context = ctx_of(ctx)
    started = context.client.post("/login/qr/start", json={"account_id": account_id})
    if not context.json_output:
        render_qr(started["payload"])
        stderr.print("Отсканируйте код в приложении MAX на телефоне.")

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        polled = context.client.post(
            "/login/qr/poll", json={"challenge_id": started["challenge_id"]}
        )
        if polled["status"] == "confirmed":
            emit(ctx, polled)
            return
        if polled["status"] == "expired":
            fail("запрос QR-входа истёк", EXIT_CONFLICT)
        time.sleep(interval)
    fail(f"подтверждение не получено за {timeout} с", EXIT_CONFLICT)


def render_qr(payload: str) -> None:
    """Печатает QR-код в терминал.

    Картинка не нужна: код рисуется символами, поэтому работает и по ssh, и в
    выводе `docker compose exec`.
    """
    code = qrcode.QRCode(border=1)
    code.add_data(payload)
    code.make(fit=True)
    code.print_ascii(out=sys.stdout)
