"""Оба способа входа в MAX поверх контракта транспорта.

Отделено от [работы с сообщениями][maxub.transport.pymax] по границе
ответственности: вход — это разговор про запросы, коды и сроки, и вместе с
отправкой он читался хуже. Состояние незавершённых входов живёт здесь же:
транспорт про него знает ровно одно — что при закрытии соединения его надо
сбросить.
"""

from __future__ import annotations

import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from maxub.core.models import LoginChallenge, QrChallenge, Session, utcnow
from maxub.transport.base import TransportAuthError
from maxub.transport.pymax_client import session_from
from maxub.transport.pymax_errors import translate
from maxub.transport.pymax_login import (
    CodeGate,
    QrGate,
    RefusePassword,
    SmsGateFlow,
    await_code_verdict,
    await_qr_link,
)
from maxub.transport.pymax_runtime import ClientRuntime

#: Сколько ждать ответа сервера на шаг входа.
LOGIN_WAIT = 60.0

#: Срок жизни запроса входа. Настоящий срок знает сервер: для QR его сторожит
#: сам PyMax и по истечении роняет вход. Здесь запас в большую сторону, чтобы
#: истечение объявлял сервер, а не наши часы.
CHALLENGE_TTL = timedelta(minutes=5)

#: Номер, под которым сохраняется сессия, полученная по QR: телефон при таком
#: входе не участвует, а поле в модели обязательное.
QR_PHONE = "qr"

#: Поднимает клиент PyMax и возвращает его фоновую жизнь. Владеет запуском
#: транспорт: вход только просит.
Launcher = Callable[..., Awaitable[ClientRuntime]]


@dataclass(slots=True)
class _PhoneLogin:
    challenge_id: str
    phone: str
    gate: CodeGate
    runtime: ClientRuntime


class LoginFlows:
    """Вход по телефону и по QR-коду."""

    def __init__(self, pymax: Any, launch: Launcher, work_dir: str) -> None:
        self._pymax = pymax
        self._launch = launch
        self._work_dir = work_dir
        self._phone: _PhoneLogin | None = None
        self._qr: tuple[str, ClientRuntime] | None = None

    def reset(self) -> None:
        """Забывает незавершённые входы: их клиент только что закрыли."""
        self._phone = None
        self._qr = None

    def owns_qr(self, challenge_id: str) -> bool:
        """Идёт ли сейчас именно этот вход по QR.

        Нужно, чтобы опоздавший опрос чужого запроса не привёл к уборке: она
        закрыла бы соединение живого входа, который к нему не относится.
        """
        return self._qr is not None and self._qr[0] == challenge_id

    # --- по телефону --------------------------------------------------------

    async def start_phone(self, phone: str) -> LoginChallenge:
        gate = CodeGate()
        runtime = await self._launch(
            lambda extra: self._pymax.Client(
                phone=phone,
                work_dir=self._work_dir,
                extra_config=extra,
                auth_flow=SmsGateFlow(gate),
            ),
            web=False,
        )
        # Ждём не «клиент запустился», а «запрос кода ушёл в MAX»: до этого
        # обещать пользователю SMS нельзя.
        await runtime.await_event(gate.requested, LOGIN_WAIT, during_auth=True)
        challenge_id = secrets.token_hex(8)
        self._phone = _PhoneLogin(challenge_id, phone, gate, runtime)
        return LoginChallenge(
            challenge_id=challenge_id, phone=phone, expires_at=utcnow() + CHALLENGE_TTL
        )

    async def complete_phone(self, challenge_id: str, code: str, account_id: int) -> Session:
        login = self._phone
        if login is None or login.challenge_id != challenge_id:
            raise TransportAuthError("запрос входа не найден, начните вход заново")
        login.gate.submit(code)
        # Неверный код прилетит сюда `TransportAuthError`, а запрос останется
        # живым: PyMax уже ждёт следующую попытку.
        await await_code_verdict(login.runtime, login.gate, LOGIN_WAIT)
        self._phone = None
        return session_from(login.runtime, account_id, phone=login.phone, kind="tcp")

    # --- по QR-коду ---------------------------------------------------------

    async def start_qr(self) -> QrChallenge:
        gate = QrGate()
        runtime = await self._launch(
            lambda extra: self._pymax.WebClient(
                work_dir=self._work_dir,
                extra_config=extra,
                auth_flow=self._pymax.QrAuthFlow(gate, RefusePassword()),
            ),
            web=True,
        )
        link = await await_qr_link(runtime, gate, LOGIN_WAIT)
        challenge_id = secrets.token_hex(8)
        self._qr = (challenge_id, runtime)
        return QrChallenge(
            challenge_id=challenge_id, payload=link, expires_at=utcnow() + CHALLENGE_TTL
        )

    async def poll_qr(self, challenge_id: str, account_id: int) -> Session | None:
        if self._qr is None or self._qr[0] != challenge_id:
            raise TransportAuthError("запрос QR-входа не найден, начните вход заново")
        runtime = self._qr[1]
        if runtime.failure is not None:
            # Срок QR сторожит сам PyMax: истечение приходит сюда ошибкой
            # фоновой задачи, и это надёжнее наших часов.
            self._qr = None
            raise translate(runtime.failure, during_auth=True)
        if not runtime.started.is_set():
            return None
        self._qr = None
        return session_from(runtime, account_id, phone=QR_PHONE, kind="web")
