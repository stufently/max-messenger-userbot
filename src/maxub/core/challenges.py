"""Реестр незавершённых запросов входа.

Начать вход может кто угодно с токеном API, а закончить — никто: пользователь
закрыл вкладку, оборвал CLI, передумал. Поэтому реестр ограничен с трёх сторон
— срок жизни берётся у транспорта, на аккаунт живёт один запрос каждого вида,
плюс потолок на процесс.

Вынесено из [auth][maxub.core.auth] отдельно: там состав операций входа, здесь
— учёт короткоживущих записей и их синхронизация.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from maxub.core.models import utcnow

#: Потолок поверх срока от транспорта — на случай, если внутренний API MAX
#: отдаст неадекватно далёкий `expires_at`.
MAX_CHALLENGE_TTL = timedelta(minutes=10)
#: Предел живых запросов в одном реестре — граница расхода памяти демона.
#: Реестра два (телефон и QR), они не мешают друг другу: пользователь вправе
#: начать вход одним способом, не отменяя начатый другим.
MAX_ACTIVE_CHALLENGES = 128
#: Сколько разобранных идентификаторов помним, чтобы на повторное обращение
#: ответить «истёк»/«уже использован», а не «неизвестный запрос».
FINISHED_MEMORY = 64

EXPIRED = "срок действия запроса входа истёк"
SUPERSEDED = "запрос входа отменён более новым"
USED = "запрос входа уже использован"


class LoginError(Exception):
    """Вход не удался по причине, которую нужно показать пользователю."""


class TooManyChallenges(LoginError):
    """Потолок незавершённых запросов входа исчерпан.

    Отдельный класс нужен адаптерам: это временная перегрузка, а не «аккаунт не
    найден», и отвечать на неё 404 было бы враньём.
    """


class ChallengeGone(LoginError):
    """Запроса входа больше нет.

    `expired` отличает «начните заново» (истёк или вытеснен новым) от «этим
    запросом уже вошли» — для QR первое становится статусом EXPIRED, второе
    остаётся ошибкой, иначе повторный опрос оживил бы завершённый вход.
    """

    def __init__(self, reason: str, account_id: int | None = None) -> None:
        super().__init__(reason)
        self.expired = reason in (EXPIRED, SUPERSEDED)
        self.account_id = account_id


@dataclass
class Challenge:
    account_id: int
    expires_at: datetime
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    #: Проставляется при изъятии из реестра: тот, кто ждал на замке, по нему
    #: узнаёт, что запрос уже разобрали, и не идёт в транспорт следом.
    outcome: str | None = None


class ChallengeRegistry:
    """Реестр незавершённых запросов входа одного вида.

    Просроченное выбрасывается при обращении, а не фоновой задачей: записей
    единицы, любое обращение и так проходит через реестр, а таск пришлось бы
    заводить, останавливать и учитывать в тестах — цена выше пользы.
    """

    def __init__(self, kind: str) -> None:
        self._kind = kind
        self._items: dict[str, Challenge] = {}
        self._finished: OrderedDict[str, tuple[str, int]] = OrderedDict()

    def prepare(self, account_id: int) -> None:
        """Освобождает место под новый запрос до обращения к транспорту.

        Проверка заранее нужна, чтобы отказ по потолку не оставлял на сервере
        MAX выданный и уже никому не нужный код. Решает всё равно `add`: между
        проверкой и ответом транспорта управление уходит в цикл событий.
        """
        self._reserve(account_id)

    def add(self, challenge_id: str, account_id: int, expires_at: datetime) -> None:
        """Регистрирует выданный запрос, вытесняя прежние запросы аккаунта."""
        self._reserve(account_id)
        deadline = min(expires_at, utcnow() + MAX_CHALLENGE_TTL)
        self._items[challenge_id] = Challenge(account_id=account_id, expires_at=deadline)

    def _reserve(self, account_id: int) -> None:
        self._sweep()
        for challenge_id, item in list(self._items.items()):
            # Пользователю нужен только последний код: держать оба — значит
            # копить записи на каждое нажатие «войти».
            if item.account_id == account_id:
                self.finish(challenge_id, SUPERSEDED)
        if len(self._items) >= MAX_ACTIVE_CHALLENGES:
            raise TooManyChallenges("слишком много незавершённых запросов входа, попробуйте позже")

    def get(self, challenge_id: str) -> Challenge:
        """Отдаёт живой запрос, попутно выбрасывая просроченные."""
        entry = self._items.get(challenge_id)
        if entry is not None and entry.expires_at <= utcnow():
            self.finish(challenge_id, EXPIRED)
            entry = None
        self._sweep()
        if entry is not None:
            return entry
        known = self._finished.get(challenge_id)
        if known is None:
            raise ChallengeGone(f"неизвестный {self._kind}")
        raise ChallengeGone(*known)

    @asynccontextmanager
    async def hold(self, challenge_id: str) -> AsyncIterator[Challenge]:
        """Выдаёт запрос под его замком.

        Параллельные обращения к одному запросу выстраиваются в очередь, и тот,
        кто дождался, видит, что запрос уже разобрали, — второй раз в транспорт
        никто не пойдёт.
        """
        entry = self.get(challenge_id)
        async with entry.lock:
            if entry.outcome is not None:
                raise ChallengeGone(entry.outcome, entry.account_id)
            if entry.expires_at <= utcnow():
                self.finish(challenge_id, EXPIRED)
                raise ChallengeGone(EXPIRED, entry.account_id)
            yield entry

    def finish(self, challenge_id: str, reason: str) -> bool:
        """Изымает запрос атомарно — `pop`, а не `get` с последующим `del`.

        `False` значит, что запрос уже разобрал кто-то другой: вытеснил новым
        или списал по сроку, пока владелец замка ждал ответа транспорта.
        """
        entry = self._items.pop(challenge_id, None)
        if entry is None:
            return False
        entry.outcome = reason
        self._finished[challenge_id] = (reason, entry.account_id)
        while len(self._finished) > FINISHED_MEMORY:
            self._finished.popitem(last=False)
        return True

    def claim(self, challenge_id: str, entry: Challenge) -> None:
        """Списывает запрос как использованный после ответа транспорта.

        Замок запроса не мешает `start_*` вытеснить его, а сроку — истечь, пока
        шёл вызов. Тогда сессию применять нельзя: вход должен подтвердить тот
        запрос, который на момент подтверждения был действующим.
        """
        if entry.expires_at <= utcnow():
            self.finish(challenge_id, EXPIRED)
            raise ChallengeGone(EXPIRED, entry.account_id)
        if not self.finish(challenge_id, USED):
            raise ChallengeGone(entry.outcome or EXPIRED, entry.account_id)

    def _sweep(self) -> None:
        now = utcnow()
        for challenge_id, item in list(self._items.items()):
            if item.expires_at <= now:
                self.finish(challenge_id, EXPIRED)
