"""Вход в MAX через точки расширения PyMax.

PyMax спрашивает код подтверждения посреди `client.start()`, а контракт
транспорта делит вход на два вызова: `start_login` и `complete_login`. Склейка
живёт здесь — в объектах, которые PyMax зовёт как обычных провайдеров, а
адаптер использует как канал между двумя вызовами.

Штатный `SmsAuthFlow` для этого не годится: он спрашивает код ровно один раз и
на неверном коде роняет весь вход. Ядро же намеренно оставляет запрос живым —
неверный код это повод повторить ввод, а не просить у MAX новый код (см.
`core/auth.py`). Поэтому цикл попыток свой, а вызовы к серверу — те же самые,
что делает `SmsAuthFlow`.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from maxub.transport.base import TransportAuthError, TransportOutcomeUnknown
from maxub.transport.pymax_errors import translate
from maxub.transport.pymax_runtime import ClientRuntime


@dataclass(slots=True)
class AuthResult:
    """Ответ auth-flow. PyMax читает у него только ``token``."""

    token: str | None


class CodeGate:
    """Канал для кода подтверждения между двумя вызовами контракта."""

    def __init__(self) -> None:
        #: Взводится, когда запрос кода уже ушёл в MAX: до этого момента
        #: `start_login` не имеет права сообщать, что код отправлен.
        self.requested = asyncio.Event()
        self._codes: asyncio.Queue[str] = asyncio.Queue()
        self._rejected: asyncio.Queue[TransportAuthError] = asyncio.Queue()

    async def next_code(self) -> str:
        """Ждёт очередной код. Вызывается изнутри PyMax."""
        self.requested.set()
        return await self._codes.get()

    def submit(self, code: str) -> None:
        self._codes.put_nowait(code)

    def reject(self, error: TransportAuthError) -> None:
        self._rejected.put_nowait(error)

    async def wait_rejection(self) -> TransportAuthError:
        return await self._rejected.get()


class QrGate:
    """Забирает ссылку QR-кода, которую PyMax отдаёт «на показ пользователю»."""

    def __init__(self) -> None:
        self.link: asyncio.Future[str] = asyncio.get_running_loop().create_future()

    async def show_qr(self, qr_url: str) -> None:
        if not self.link.done():
            self.link.set_result(qr_url)


class RefusePassword:
    """Провайдер пароля 2FA, который сразу отказывает.

    Штатный `ConsolePasswordProvider` читает пароль из stdin, а в демоне stdin
    никого не ждёт: вход завис бы навсегда. Отказ здесь честнее — аккаунт с 2FA
    этим адаптером пока не поддерживается.
    """

    async def get_password(self, hint: str | None = None) -> str:
        raise TransportAuthError("аккаунт защищён паролем 2FA: адаптер pymax его не поддерживает")


class RefuseAuth:
    """Auth-flow для восстановления сессии по сохранённому токену.

    Если PyMax дошёл сюда, значит токена не оказалось, и вместо тихого входа
    заново (SMS в консоль, QR в никуда) нужен явный отказ.
    """

    async def authenticate(self, app: Any) -> AuthResult:
        raise TransportAuthError("сохранённая сессия не принята MAX: нужен новый вход")


class SmsGateFlow:
    """Вход по коду из SMS с несколькими попытками ввода."""

    def __init__(self, gate: CodeGate) -> None:
        self._gate = gate

    async def authenticate(self, app: Any) -> AuthResult:
        phone = app.config.phone
        if not phone:
            raise TransportAuthError("вход по телефону запрошен без номера")
        start = await app.api.auth.request_code(phone)
        while True:
            code = await self._gate.next_code()
            result = await self._send_code(app, start.token, code)
            if result is None:
                continue
            if result.login_token:
                return AuthResult(token=result.login_token)
            if result.password_challenge is not None:
                raise TransportAuthError(
                    "аккаунт защищён паролем 2FA: адаптер pymax его не поддерживает"
                )
            if result.register_token:
                # Регистрация новых аккаунтов в проект не входит осознанно.
                raise TransportAuthError("номер не зарегистрирован в MAX")
            self._gate.reject(TransportAuthError("MAX не принял код подтверждения"))

    async def _send_code(self, app: Any, token: str, code: str) -> Any | None:
        """Отправляет код. ``None`` означает «отказ, ждём следующую попытку»."""
        try:
            return await app.api.auth.send_code(token, code)
        except Exception as exc:
            error = translate(exc, during_auth=True)
            if not isinstance(error, TransportAuthError):
                # Обрыв связи повторным вводом кода не лечится: запрос кода
                # придётся начинать заново, поэтому вход завершается ошибкой.
                raise error from exc
            self._gate.reject(error)
            return None


async def await_code_verdict(runtime: ClientRuntime, gate: CodeGate, wait_seconds: float) -> None:
    """Ждёт, чем кончился разбор кода: входом или отказом.

    Отказ ждётся отдельно от смерти клиента, потому что вход при этом остаётся
    живым: PyMax возвращается за следующим кодом, а ядро намеренно сохраняет
    запрос, чтобы пользователь повторил ввод.
    """
    rejected = asyncio.ensure_future(gate.wait_rejection())
    started = asyncio.ensure_future(runtime.started.wait())
    watched: set[asyncio.Future[Any]] = {rejected, started}
    if runtime.task is not None:
        watched.add(runtime.task)
    try:
        await asyncio.wait(watched, timeout=wait_seconds, return_when=asyncio.FIRST_COMPLETED)
    except BaseException:
        # Отказ, вынутый из очереди ровно в момент отмены, возвращается назад:
        # иначе следующая попытка ввода кода ждала бы вердикт, который уже
        # некому выдать.
        if rejected.done() and not rejected.cancelled():
            gate.reject(rejected.result())
        rejected.cancel()
        started.cancel()
        raise
    try:
        if runtime.failure is not None:
            raise translate(runtime.failure, during_auth=True)
        if rejected.done():
            raise rejected.result()
        if not runtime.started.is_set():
            raise TransportOutcomeUnknown(f"MAX не ответил на код за {wait_seconds:.0f} с")
    finally:
        rejected.cancel()
        started.cancel()


async def await_qr_link(runtime: ClientRuntime, gate: QrGate, wait_seconds: float) -> str:
    """Ждёт ссылку QR-кода — или смерти входа вместо неё."""
    watched: set[asyncio.Future[Any]] = {gate.link}
    if runtime.task is not None:
        watched.add(runtime.task)
    await asyncio.wait(watched, timeout=wait_seconds, return_when=asyncio.FIRST_COMPLETED)
    if runtime.failure is not None:
        raise translate(runtime.failure, during_auth=True)
    if not gate.link.done():
        raise TransportOutcomeUnknown(f"MAX не выдал QR-код за {wait_seconds:.0f} с")
    return gate.link.result()
