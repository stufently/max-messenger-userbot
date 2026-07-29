"""Раскладка исключений PyMax по таксономии транспорта.

Это самая дорогая часть адаптера: от неё зависит, повторит ли ядро отправку.
Ошибиться в сторону «точно не выполнено» страшнее всего — получатель увидит
дубль, и отменить это уже нельзя.

Отсюда главное решение: сам факт отказа сервера невыполнения ещё не доказывает.
`ApiError` в PyMax означает только «на наш запрос пришёл ответ с признаком
ошибки»; какие коды MAX присылает и что каждый из них означает, не описано
нигде. Поэтому неопознанный отказ — это `TransportOutcomeUnknown`, и сообщение
уходит человеку. В `TransportNotApplied` и его подкласс `TransportRateLimited`
попадает только то, что опознано по маркерам ниже.

Маркеры подобраны по смыслу, а не по документации — её нет. Это первое, что
нужно выверить на живом аккаунте, см. докстринг [адаптера][maxub.transport.pymax].
"""

from __future__ import annotations

from typing import Any

from maxub.transport.base import (
    TransportAuthError,
    TransportError,
    TransportOutcomeUnknown,
    TransportPermanent,
    TransportRateLimited,
)

#: Признаки того, что отказ вызван частотой обращений, а не самим запросом.
RATE_MARKERS = ("flood", "too.many", "too_many", "toomany", "throttl", "rate.limit", "ratelimit")

#: Признаки того, что сессия больше не годится и нужен повторный вход.
AUTH_MARKERS = (
    "unauthor",
    "not.authorized",
    "login.token",
    "token.invalid",
    "invalid.token",
    "token.expired",
    "session.invalid",
    "invalid.session",
    "session.expired",
    "auth.required",
    "verify.code",
    "wrong.code",
)

#: Признаки того, что запрос неверен сам по себе: повтор не поможет, и до
#: применения дело точно не дошло — сервер отверг его на разборе. Маркеры
#: намеренно узкие: одинокое «invalid» накрыло бы и отозванную сессию, а
#: закрыть проблему авторизации как «неверный запрос» — потерять аккаунт из
#: виду до перезапуска.
PERMANENT_MARKERS = (
    "not.found",
    "notfound",
    "chat.not",
    "invalid.payload",
    "invalid.request",
    "invalid.chat",
    "bad.request",
    "too.long",
    "toolong",
    "forbidden",
    "no.access",
)

#: Ключи, которыми сервер может подсказать паузу.
RETRY_KEYS = ("retryAfter", "retry_after", "retryAfterMs", "retry_after_ms")

#: Для ключей без явного суффикса значение больше этого порога считаем
#: миллисекундами: пауза длиной в 17 минут для мессенджера неправдоподобна, а
#: миллисекунды — обычный формат для внутренних API.
MILLISECOND_THRESHOLD = 1000.0


def translate(exc: BaseException, *, during_auth: bool = False) -> TransportError:
    """Переводит исключение PyMax в ошибку транспорта.

    ``during_auth`` меняет разбор отказов сервера: во время входа и
    восстановления сессии отказ по определению относится к авторизации, а не к
    отправке, и ядру нужен именно `TransportAuthError` — он ведёт аккаунт в
    `AUTH_REQUIRED`, а не в бесконечный повтор.
    """
    if isinstance(exc, TransportError):
        # Наши собственные ошибки (например, отказ провайдера пароля 2FA)
        # проходят сквозь PyMax насквозь и уже разложены.
        return exc
    api = _as_api_error(exc)
    if api is not None:
        return _from_api_error(api, during_auth=during_auth)
    if isinstance(exc, TimeoutError | ConnectionError | EOFError | OSError):
        # Запрос ушёл в сеть, ответа нет. Сообщение могло дойти до получателя —
        # это ровно тот случай, ради которого заведён «исход неизвестен».
        return TransportOutcomeUnknown(f"связь с MAX прервалась: {exc}")
    if during_auth and isinstance(exc, RuntimeError):
        # PyMax сообщает о неудачном входе именно RuntimeError: истёкший QR,
        # ответ сервера без токена, отсутствие телефона.
        return TransportAuthError(f"вход не завершён: {exc}")
    if _is_pymax_error(exc):
        # Ответ от сервера пришёл, но библиотека не смогла его разобрать. Для
        # отправки это значит «сервер, скорее всего, сообщение принял».
        return TransportOutcomeUnknown(f"PyMax не разобрал ответ MAX: {exc}")
    return TransportOutcomeUnknown(f"неопознанный сбой PyMax: {type(exc).__name__}: {exc}")


def _from_api_error(exc: Any, *, during_auth: bool) -> TransportError:
    """Разбирает отказ, который сервер прислал явным ответом.

    Опознанный отказ говорит, до какой стадии дело не дошло, и позволяет
    повторить. Неопознанный не говорит ничего: он может означать и «не принял»,
    и «принял, но ответить нормально не смог».
    """
    text = " ".join(
        str(part)
        for part in (
            getattr(exc, "error", None),
            getattr(exc, "message", None),
            getattr(exc, "localized_message", None),
        )
        if part
    ).lower()
    if any(marker in text for marker in RATE_MARKERS):
        # Ограничение частоты сервер применяет до разбора команды: она заведомо
        # не выполнена, и повтор после паузы безопасен. Проверяется раньше
        # `during_auth` намеренно: лимит на входе — это «подожди», а не
        # «сессия испорчена», и вести аккаунт в AUTH_REQUIRED из-за него
        # значило бы требовать нового входа там, где хватит паузы.
        return TransportRateLimited(
            f"MAX ограничил частоту обращений: {exc}",
            retry_after=_retry_after(getattr(exc, "payload", None)),
        )
    if during_auth or any(marker in text for marker in AUTH_MARKERS):
        # Прочий отказ во время входа и восстановления сессии относится к
        # авторизации: ядру нужен AUTH_REQUIRED, а не повтор.
        return TransportAuthError(f"MAX отклонил авторизацию: {exc}")
    if any(marker in text for marker in PERMANENT_MARKERS):
        return TransportPermanent(f"MAX отклонил запрос как неверный: {exc}")
    return TransportOutcomeUnknown(f"MAX отказал без объяснения, исход неизвестен: {exc}")


def _retry_after(payload: Any) -> float | None:
    if not isinstance(payload, dict):
        return None
    for key in RETRY_KEYS:
        value = payload.get(key)
        if not isinstance(value, int | float) or isinstance(value, bool) or value <= 0:
            continue
        if key.lower().endswith("ms"):
            # Единицы названы в самом ключе — гадать не о чем.
            return float(value) / 1000
        return float(value) / 1000 if value > MILLISECOND_THRESHOLD else float(value)
    return None


def _as_api_error(exc: BaseException) -> Any | None:
    """Опознаёт `pymax.ApiError`, не импортируя библиотеку заранее.

    Импорт отложен по той же причине, что и в самом адаптере: модуль обязан
    грузиться и без установленного PyMax, иначе реестр транспортов не соберётся.
    """
    api_error = _pymax_class("ApiError")
    if api_error is not None:
        return exc if isinstance(exc, api_error) else None
    return None


def _is_pymax_error(exc: BaseException) -> bool:
    base = _pymax_class("PyMaxError")
    return base is not None and isinstance(exc, base)


def _pymax_class(name: str) -> type[BaseException] | None:
    try:
        import pymax
    except ImportError:  # pragma: no cover - без библиотеки сюда не доходят
        return None
    found = getattr(pymax, name, None)
    return found if isinstance(found, type) and issubclass(found, BaseException) else None
