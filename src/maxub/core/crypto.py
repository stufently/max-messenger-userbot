"""Шифрование секретов, хранимых на диске.

Сессии аккаунтов — это полный доступ к переписке, поэтому в БД они лежат
зашифрованными. Ключ хранится **вне** базы: либо в переменной окружения, либо в
отдельном файле с правами 0600. Кража одного файла БД без ключа бесполезна.

Ограничение текущей схемы: ключевой файл лежит рядом с базой, то есть защищает
от утечки бэкапа или копии тома, но не от злоумышленника с доступом к тому же
каталогу. Привязка к хранилищам ОС (Windows DPAPI, Linux Secret Service) — в
[TASKS.md](../../../TASKS.md).
"""

from __future__ import annotations

import json
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

ENVELOPE_VERSION = 1


class SecretError(Exception):
    """Не удалось расшифровать секрет: не тот ключ или повреждённые данные."""


def generate_key() -> str:
    """Новый ключ шифрования в виде строки."""
    return Fernet.generate_key().decode("ascii")


class SecretBox:
    """Шифрует и расшифровывает структуры данных.

    Наружу отдаётся конверт с номером версии — он понадобится, когда схема
    шифрования поменяется и старые записи придётся читать по-старому.
    """

    def __init__(self, key: str) -> None:
        try:
            self._fernet = Fernet(key.encode("ascii"))
        except (ValueError, TypeError) as exc:
            raise SecretError("некорректный ключ шифрования") from exc

    def seal(self, payload: dict[str, Any]) -> str:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        token = self._fernet.encrypt(raw).decode("ascii")
        return json.dumps({"v": ENVELOPE_VERSION, "token": token})

    def open(self, envelope: str) -> dict[str, Any]:
        try:
            parsed = json.loads(envelope)
        except json.JSONDecodeError as exc:
            raise SecretError("повреждённый конверт секрета") from exc
        if not isinstance(parsed, dict) or "token" not in parsed:
            raise SecretError("конверт секрета без полезной нагрузки")
        version = parsed.get("v")
        if version != ENVELOPE_VERSION:
            raise SecretError(f"неизвестная версия конверта: {version}")
        try:
            raw = self._fernet.decrypt(str(parsed["token"]).encode("ascii"))
        except InvalidToken as exc:
            raise SecretError("не удалось расшифровать секрет: не тот ключ") from exc
        decoded: dict[str, Any] = json.loads(raw.decode("utf-8"))
        return decoded
