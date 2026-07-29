"""Заглушечный транспорт.

Нужен, чтобы весь путь «CLI → API → ядро → транспорт» проверялся в Docker без
реального аккаунта MAX и без обращений к внутреннему API. Реальный адаптер
подключается после spike-теста, см. TASKS.md.

Заглушка ведёт общий журнал сообщений: это позволяет проверять и добор
пропущенного по курсору, и поиск отправленного по клиентскому токену.
"""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import AsyncIterator
from datetime import timedelta

from maxub.core.models import LoginChallenge, Message, QrChallenge, Session, utcnow
from maxub.transport.base import (
    Capabilities,
    ReconcileOutcome,
    ReconcileResult,
    TransportAuthError,
    TransportNotApplied,
    Update,
)

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
        backfill=True,
        reconcile=True,
        qr_login=True,
    )

    def __init__(self) -> None:
        self._challenges: dict[str, str] = {}
        self._qr_challenges: dict[str, bool] = {}
        self._connected = False
        self._journal: list[Update] = []
        self._client_tokens: dict[str, str] = {}
        self._incoming: asyncio.Queue[Update] = asyncio.Queue()
        self._counter = 0
        #: Сколько раз send_text завершится ошибкой перед успехом — для тестов.
        self.fail_sends = 0
        self.fail_with: Exception | None = None
        #: Заставляет сверку отвечать «выяснить не удалось» — для тестов.
        self.reconcile_inconclusive = False
        #: Заставляет ближайшее подключение вернуть обновлённую сессию.
        self.rotate_token_on_connect = False

    # --- авторизация --------------------------------------------------------

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

    async def start_qr_login(self) -> QrChallenge:
        challenge_id = secrets.token_hex(8)
        self._qr_challenges[challenge_id] = False
        return QrChallenge(
            challenge_id=challenge_id,
            payload=f"maxub-stub-qr://{challenge_id}",
            expires_at=utcnow() + CHALLENGE_TTL,
        )

    async def poll_qr_login(self, challenge_id: str, account_id: int) -> Session | None:
        confirmed = self._qr_challenges.get(challenge_id)
        if confirmed is None:
            raise TransportAuthError("неизвестный или истёкший запрос QR-входа")
        if not confirmed:
            return None
        del self._qr_challenges[challenge_id]
        return Session(
            account_id=account_id,
            phone="qr",
            token=f"stub-{secrets.token_hex(8)}",
            device_id=f"stub-device-{account_id}",
        )

    def confirm_qr(self, challenge_id: str) -> None:
        """Имитирует подтверждение входа с телефона."""
        if challenge_id not in self._qr_challenges:
            raise TransportAuthError("неизвестный запрос QR-входа")
        self._qr_challenges[challenge_id] = True

    async def connect(self, session: Session) -> Session | None:
        if not session.token.startswith("stub-"):
            raise TransportAuthError("сессия не принадлежит заглушечному транспорту")
        self._connected = True
        if not self.rotate_token_on_connect:
            return None
        # Имитация ротации токена сервером: ядро обязано сохранить новую сессию,
        # иначе следующий запуск придёт со старой и попросит войти заново.
        self.rotate_token_on_connect = False
        return session.model_copy(update={"token": f"stub-{secrets.token_hex(8)}"})

    async def disconnect(self) -> None:
        self._connected = False

    # --- сообщения ----------------------------------------------------------

    async def send_text(self, chat_id: str, text: str, client_token: str) -> str:
        self._require_connection()
        if self.fail_sends > 0:
            self.fail_sends -= 1
            error = self.fail_with or TransportAuthError("имитация сбоя отправки")
            if not isinstance(error, TransportNotApplied):
                # Исход неизвестен: сообщение всё же попадает в журнал — так
                # воспроизводится худший случай «доставлено, но подтверждение
                # не дошло». Для TransportNotApplied обратное: не записываем,
                # потому что этот тип ошибки означает «точно не выполнено».
                self._record(chat_id, text, outgoing=True, client_token=client_token)
            raise error
        message = self._record(chat_id, text, outgoing=True, client_token=client_token)
        return message.remote_id

    async def fetch_history(self, chat_id: str, limit: int) -> list[Message]:
        self._require_connection()
        return [u.message for u in self._journal if u.message.chat_id == chat_id][-limit:]

    async def fetch_updates(
        self, cursor: str | None, limit: int
    ) -> tuple[list[Update], str | None]:
        self._require_connection()
        start = 0
        if cursor is not None:
            for index, update in enumerate(self._journal):
                if update.cursor == cursor:
                    start = index + 1
                    break
        chunk = self._journal[start : start + limit]
        if not chunk:
            return [], cursor
        return chunk, chunk[-1].cursor

    async def reconcile_send(self, chat_id: str, client_token: str) -> ReconcileResult:
        self._require_connection()
        if self.reconcile_inconclusive:
            return ReconcileResult(
                outcome=ReconcileOutcome.INCONCLUSIVE, detail="имитация неполного поиска"
            )
        remote_id = self._client_tokens.get(client_token)
        if remote_id is not None:
            for update in self._journal:
                if update.message.remote_id == remote_id and update.message.chat_id == chat_id:
                    return ReconcileResult(outcome=ReconcileOutcome.FOUND, message=update.message)
        return ReconcileResult(outcome=ReconcileOutcome.NOT_FOUND)

    async def events(self) -> AsyncIterator[Update]:
        while True:
            yield await self._incoming.get()

    # --- утилиты для тестов -------------------------------------------------

    async def push_incoming(self, chat_id: str, text: str, sender_id: str = "stub-peer") -> Message:
        """Имитирует входящее сообщение."""
        update = self._record_update(chat_id, text, outgoing=False, sender_id=sender_id)
        await self._incoming.put(update)
        return update.message

    def add_missed(self, chat_id: str, text: str, sender_id: str = "stub-peer") -> Message:
        """Кладёт сообщение только в журнал, минуя поток событий.

        Так воспроизводится пропуск: пока демон был отключён, сообщение пришло,
        и добрать его можно только по курсору.
        """
        return self._record(chat_id, text, outgoing=False, sender_id=sender_id)

    # --- внутреннее ---------------------------------------------------------

    def _require_connection(self) -> None:
        if not self._connected:
            raise TransportAuthError("транспорт не подключён")

    def _record(
        self,
        chat_id: str,
        text: str,
        *,
        outgoing: bool,
        sender_id: str | None = None,
        client_token: str | None = None,
    ) -> Message:
        return self._record_update(
            chat_id,
            text,
            outgoing=outgoing,
            sender_id=sender_id,
            client_token=client_token,
        ).message

    def _record_update(
        self,
        chat_id: str,
        text: str,
        *,
        outgoing: bool,
        sender_id: str | None = None,
        client_token: str | None = None,
    ) -> Update:
        self._counter += 1
        prefix = "out" if outgoing else "in"
        message = Message(
            remote_id=f"stub-{prefix}-{self._counter}",
            chat_id=chat_id,
            sender_id=sender_id,
            text=text,
            outgoing=outgoing,
        )
        # Позиция намеренно живёт в отдельном пространстве значений от
        # `remote_id`: так проверяется, что ядро нигде их не путает.
        update = Update(message=message, cursor=f"seq-{self._counter}")
        self._journal.append(update)
        if client_token is not None:
            self._client_tokens[client_token] = message.remote_id
        return update
