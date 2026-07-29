"""Заглушечный транспорт.

Нужен, чтобы весь путь «CLI → API → ядро → транспорт» проверялся в Docker без
реального аккаунта MAX и без обращений к внутреннему API. Реальный адаптер
подключается после spike-теста, см. TASKS.md.
"""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import AsyncIterator
from datetime import timedelta

from maxub.core.models import LoginChallenge, Message, Session, utcnow
from maxub.transport.base import Capabilities, TransportAuthError

STUB_CODE = "00000"
CHALLENGE_TTL = timedelta(minutes=5)


class StubTransport:
    """Транспорт в памяти. Код подтверждения фиксирован: ``00000``."""

    name = "stub"
    capabilities = Capabilities(
        send_text=True,
        fetch_history=True,
        edit_message=False,
        delete_message=False,
        media=False,
    )

    def __init__(self) -> None:
        self._challenges: dict[str, str] = {}
        self._connected = False
        self._sent: list[Message] = []
        self._incoming: asyncio.Queue[Message] = asyncio.Queue()
        self._counter = 0

    async def start_login(self, phone: str) -> LoginChallenge:
        challenge_id = secrets.token_hex(8)
        self._challenges[challenge_id] = phone
        return LoginChallenge(
            challenge_id=challenge_id,
            phone=phone,
            expires_at=utcnow() + CHALLENGE_TTL,
        )

    async def complete_login(self, challenge_id: str, code: str, account_id: int) -> Session:
        phone = self._challenges.get(challenge_id)
        if phone is None:
            raise TransportAuthError("неизвестный challenge_id")
        if code != STUB_CODE:
            raise TransportAuthError("неверный код подтверждения")
        del self._challenges[challenge_id]
        return Session(
            account_id=account_id,
            phone=phone,
            token=f"stub-{secrets.token_hex(8)}",
            device_id=f"stub-device-{account_id}",
        )

    async def connect(self, session: Session) -> None:
        if not session.token.startswith("stub-"):
            raise TransportAuthError("сессия не принадлежит заглушечному транспорту")
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False

    async def send_text(self, chat_id: str, text: str) -> str:
        if not self._connected:
            raise TransportAuthError("транспорт не подключён")
        self._counter += 1
        remote_id = f"stub-msg-{self._counter}"
        self._sent.append(Message(remote_id=remote_id, chat_id=chat_id, text=text, outgoing=True))
        return remote_id

    async def fetch_history(self, chat_id: str, limit: int) -> list[Message]:
        if not self._connected:
            raise TransportAuthError("транспорт не подключён")
        return [m for m in self._sent if m.chat_id == chat_id][-limit:]

    async def push_incoming(self, chat_id: str, text: str, sender_id: str = "stub-peer") -> Message:
        """Утилита для тестов: имитирует входящее сообщение."""
        self._counter += 1
        message = Message(
            remote_id=f"stub-in-{self._counter}",
            chat_id=chat_id,
            sender_id=sender_id,
            text=text,
        )
        await self._incoming.put(message)
        return message

    async def events(self) -> AsyncIterator[Message]:
        while True:
            yield await self._incoming.get()
