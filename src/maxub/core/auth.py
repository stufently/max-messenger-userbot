"""Авторизация аккаунта: по номеру телефона и по QR-коду.

Оба способа приводят к одному результату — сессии, которую дальше использует
[ConnectionManager][maxub.core.sync.ConnectionManager]. Учёт незавершённых
запросов входа вынесен в [challenges][maxub.core.challenges]: запросы
короткоживущие, держатся в памяти процесса и переживать перезапуск им незачем.
"""

from __future__ import annotations

from maxub.core.challenges import (
    EXPIRED,
    Challenge,
    ChallengeGone,
    ChallengeRegistry,
    LoginError,
    TooManyChallenges,
)
from maxub.core.models import Account, AccountState, QrStatus, Session
from maxub.core.ports import AccountRepository
from maxub.core.sync import ConnectionManager
from maxub.transport.base import TransportAuthError, TransportUnsupported

__all__ = ["ChallengeGone", "LoginError", "LoginService", "TooManyChallenges"]


class LoginService:
    def __init__(self, repo: AccountRepository, connections: ConnectionManager) -> None:
        self._repo = repo
        self._connections = connections
        self._phone = ChallengeRegistry("challenge_id")
        self._qr = ChallengeRegistry("запрос QR-входа")

    # --- по номеру телефона -------------------------------------------------

    async def start_phone(self, account: Account) -> str:
        transport = self._connections.ensure(account.id)
        self._phone.prepare(account.id)
        challenge = await transport.start_login(account.phone)
        self._phone.add(challenge.challenge_id, account.id, challenge.expires_at)
        await self._repo.set_account_state(account.id, AccountState.AUTH_REQUIRED)
        return challenge.challenge_id

    async def complete_phone(self, challenge_id: str, code: str) -> tuple[int, Session]:
        async with self._phone.hold(challenge_id) as entry:
            account_id = entry.account_id
            transport = self._connections.ensure(account_id)
            try:
                session = await transport.complete_login(challenge_id, code, account_id)
            except TransportAuthError as exc:
                # Запрос остаётся живым: неверный код — повод повторить ввод, а
                # не запрашивать у MAX новый код.
                await self._fail(account_id, exc)
                raise LoginError(str(exc)) from exc
            self._phone.claim(challenge_id, entry)
            return account_id, session

    # --- по QR-коду ---------------------------------------------------------

    async def start_qr(self, account_id: int) -> dict[str, object]:
        """Начинает вход по QR-коду.

        Второй способ входа: приложение MAX на телефоне сканирует код, и SMS не
        требуется. Номер телефона в аккаунте при этом не используется.
        """
        transport = self._connections.ensure(account_id)
        if not transport.capabilities.qr_login:
            raise TransportUnsupported("транспорт не поддерживает вход по QR-коду")
        self._qr.prepare(account_id)
        challenge = await transport.start_qr_login()
        self._qr.add(challenge.challenge_id, account_id, challenge.expires_at)
        await self._repo.set_account_state(account_id, AccountState.AUTH_REQUIRED)
        return challenge.model_dump(mode="json")

    async def poll_qr(self, challenge_id: str) -> tuple[QrStatus, int | None, Session | None]:
        """Проверяет, подтверждён ли вход с телефона.

        Опрашивать один запрос могут сразу и веб-интерфейс, и CLI, поэтому
        обращение к транспорту сериализовано замком запроса: подтверждение
        должно сработать ровно один раз.
        """
        try:
            async with self._qr.hold(challenge_id) as entry:
                return await self._poll_held(challenge_id, entry)
        except ChallengeGone as exc:
            if not exc.expired:
                raise
            return QrStatus.EXPIRED, exc.account_id, None

    async def _poll_held(
        self, challenge_id: str, entry: Challenge
    ) -> tuple[QrStatus, int | None, Session | None]:
        account_id = entry.account_id
        transport = self._connections.ensure(account_id)
        try:
            session = await transport.poll_qr_login(challenge_id, account_id)
        except TransportAuthError as exc:
            self._qr.finish(challenge_id, EXPIRED)
            await self._fail(account_id, exc)
            return QrStatus.EXPIRED, account_id, None
        if session is None:
            return QrStatus.PENDING, account_id, None
        self._qr.claim(challenge_id, entry)
        return QrStatus.CONFIRMED, account_id, session

    async def _fail(self, account_id: int, exc: Exception) -> None:
        await self._repo.set_account_state(account_id, AccountState.AUTH_REQUIRED, str(exc))
