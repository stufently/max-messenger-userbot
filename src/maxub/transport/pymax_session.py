"""Хранение сессии PyMax в доменной модели `Session`.

PyMax восстанавливает сессию по четырём величинам: токен, идентификатор
устройства, `mt_instance_id` и user-agent. В `Session` полей столько нет, а
модель принадлежит ядру и меняться отсюда не может. Поэтому всё, кроме
`device_id`, уезжает конвертом в поле `token`: для ядра это по-прежнему
непрозрачная строка, которую оно шифрует на диске, а разбирает её только этот
модуль.

User-agent сохраняется намеренно. Он описывает «устройство», с которого MAX
видит аккаунт; если генерировать его заново на каждое переподключение, аккаунт
начнёт выглядеть как чехарда из десятка разных телефонов — ровно то поведение,
за которое банят.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from maxub.transport.base import TransportAuthError

ENVELOPE_VERSION = 1

#: Каким клиентом PyMax получена сессия. Токен, выданный веб-входу по QR, живёт
#: с web-подписью устройства, и подключаться им по TCP как «андроидом» нельзя.
KINDS = ("tcp", "web")


@dataclass(frozen=True, slots=True)
class Envelope:
    kind: str
    token: str
    mt_instance_id: str
    user_agent: dict[str, Any] | None


def encode(envelope: Envelope) -> str:
    return json.dumps(
        {
            "v": ENVELOPE_VERSION,
            "kind": envelope.kind,
            "token": envelope.token,
            "mt": envelope.mt_instance_id,
            "ua": envelope.user_agent,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def decode(raw: str) -> Envelope:
    """Разбирает конверт, отказывая всему, что на него не похоже.

    Отказ — `TransportAuthError`: для ядра это «сессия не годится, нужен новый
    вход», а не сбой соединения, который стоило бы повторять.
    """
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise TransportAuthError("сессия сохранена не транспортом pymax") from exc
    if not isinstance(data, dict) or data.get("v") != ENVELOPE_VERSION:
        raise TransportAuthError("формат сессии pymax не распознан, нужен повторный вход")
    kind = data.get("kind")
    token = data.get("token")
    if kind not in KINDS or not isinstance(token, str) or not token:
        raise TransportAuthError("в сессии pymax нет пригодного токена")
    mt_instance_id = data.get("mt")
    user_agent = data.get("ua")
    return Envelope(
        kind=kind,
        token=token,
        mt_instance_id=mt_instance_id if isinstance(mt_instance_id, str) else "",
        user_agent=user_agent if isinstance(user_agent, dict) else None,
    )


class MemorySessionStore:
    """Подменяет SQLite-хранилище PyMax на перехват в памяти.

    Своего файла сессий у адаптера быть не должно: сессии — секреты, и хранит
    их ядро, в одном месте и в зашифрованном виде. Заодно это единственный
    публичный способ увидеть токен: наружу PyMax его не возвращает, но исправно
    отдаёт хранилищу.

    Реализует `pymax.session.StoreProtocol`; ротацию токена обязательно ловить
    здесь же, иначе после обновления на сервере в базе останется прежний.
    """

    def __init__(self) -> None:
        self.saved: Any | None = None

    async def save_session(self, session_info: Any) -> None:
        self.saved = session_info

    async def update_token(self, old_token: str, new_token: str) -> None:
        if self.saved is not None and getattr(self.saved, "token", None) == old_token:
            self.saved = self.saved.model_copy(update={"token": new_token})

    async def load_session(self) -> Any | None:
        # Пустой ответ на старте обязателен: он говорит PyMax «сохранённой
        # сессии нет», и дальше решает уже адаптер — токеном из конверта или
        # полноценным входом.
        return self.saved

    async def load_session_by_device_id(self, device_id: str) -> Any | None:
        return self.saved if getattr(self.saved, "device_id", None) == device_id else None

    async def load_session_by_phone(self, phone: str) -> Any | None:
        return self.saved if getattr(self.saved, "phone", None) == phone else None

    async def delete_session(self, token: str) -> None:
        if getattr(self.saved, "token", None) == token:
            self.saved = None

    async def close(self) -> None:
        return None
